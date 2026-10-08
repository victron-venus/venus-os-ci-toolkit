package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ossf/scorecard/v5/checker"
	"github.com/ossf/scorecard/v5/checks/evaluation"
	"github.com/ossf/scorecard/v5/clients"
	checkdocs "github.com/ossf/scorecard/v5/docs/checks"
	"github.com/ossf/scorecard/v5/finding"
	"github.com/ossf/scorecard/v5/pkg/scorecard"
	"github.com/ossf/scorecard/v5/policy"
	"github.com/ossf/scorecard/v5/probes/packagedWithAutomatedWorkflow"
)

func completeResult() scorecard.Result {
	result := scorecard.Result{Date: time.Unix(1, 0), Repo: scorecard.RepoInfo{Name: "github.com/owner/repo", CommitSHA: strings.Repeat("a", 40)}}
	_, names := reportingPolicy()
	for _, name := range names {
		score := 10
		if thresholds[name] < 0 {
			score = -1 // Disabled upstream policies may be legitimately inconclusive.
		}
		result.Checks = append(result.Checks, checker.CheckResult{Name: name, Score: score})
	}
	return result
}

func TestReportingPolicyRunsAllDefaultGitHubChecks(t *testing.T) {
	expected, err := policy.GetEnabled(nil, nil, nil, clients.RepoTypeGitHub)
	if err != nil {
		t.Fatal(err)
	}
	if len(expected) != len(thresholds) {
		t.Fatalf("reporting policy covers %d checks, upstream defaults have %d", len(thresholds), len(expected))
	}
	for name := range expected {
		if _, exists := thresholds[name]; !exists {
			t.Errorf("upstream check %q is missing from reporting policy", name)
		}
	}
}

// Fixture copied verbatim from the pinned upstream action linked in main.go.
func TestReportingPolicyMatchesUpstreamAction(t *testing.T) {
	expected, err := policy.ParseFromFile("testdata/upstream-policy.yml")
	if err != nil {
		t.Fatal(err)
	}
	actual, _ := reportingPolicy()
	if actual.Version != expected.Version || len(actual.Policies) != len(expected.Policies) {
		t.Fatal("changed upstream policy version or check inventory")
	}
	for name, want := range expected.Policies {
		got, ok := actual.Policies[name]
		if !ok || got.Score != want.Score || got.Mode != want.Mode {
			t.Errorf("changed upstream policy for %s: got %v, want %v", name, got, want)
		}
	}
}

func TestIncompleteScanNeverPublishesOrKeepsStaleOutput(t *testing.T) {
	cases := []struct {
		name   string
		change func(*scorecard.Result)
		err    error
	}{
		{name: "API failure", err: errors.New("API unavailable")},
		{name: "wrong commit", change: func(r *scorecard.Result) { r.Repo.CommitSHA = strings.Repeat("b", 40) }},
		{name: "empty", change: func(r *scorecard.Result) { r.Checks = nil }},
		{name: "missing", change: func(r *scorecard.Result) { r.Checks = r.Checks[1:] }},
		{name: "duplicate", change: func(r *scorecard.Result) { r.Checks = append(r.Checks, r.Checks[0]) }},
		{name: "unexpected", change: func(r *scorecard.Result) { r.Checks[0].Name = "unknown" }},
		{name: "inconclusive", change: func(r *scorecard.Result) { r.Checks[0].Score = -1 }},
		{name: "invalid negative score", change: func(r *scorecard.Result) { r.Checks[0].Score = -2 }},
		{name: "invalid high score", change: func(r *scorecard.Result) { r.Checks[0].Score = 11 }},
		{name: "positive score with error", change: func(r *scorecard.Result) { r.Checks[0].Error = errors.New("rate limited") }},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			output := filepath.Join(t.TempDir(), "results.sarif")
			if err := os.WriteFile(output, []byte("stale previous result"), 0600); err != nil {
				t.Fatal(err)
			}
			result := completeResult()
			if tt.change != nil {
				tt.change(&result)
			}
			calls := 0
			err := scanAndWrite(context.Background(), output, strings.Repeat("a", 40), func(context.Context) (scorecard.Result, error) {
				calls++
				return result, tt.err
			})
			if err == nil || calls != 1 {
				t.Fatalf("incomplete scan accepted or scanned more than once: calls=%d err=%v", calls, err)
			}
			if _, err := os.Stat(output); !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("failed scan left uploadable SARIF: %v", err)
			}
		})
	}
}

func officialSARIFResult(t *testing.T, failing bool) scorecard.Result {
	t.Helper()
	result := completeResult()
	for i := range result.Checks {
		if result.Checks[i].Name == "Packaging" {
			result.Checks[i] = upstreamPackagingAbsent(t)
		}
	}
	if failing {
		for i := range result.Checks {
			if result.Checks[i].Name == "Pinned-Dependencies" {
				result.Checks[i].Score = 0
				result.Checks[i].Details = []checker.CheckDetail{{Type: checker.DetailWarn, Msg: checker.LogMessage{
					Text: "dependency is not pinned", Path: ".github/workflows/build.yml", Type: finding.FileTypeSource, Offset: 7,
				}}}
			}
		}
	}
	return result
}

func assertOfficialSARIFRun(t *testing.T, run map[string]interface{}, categories map[string]bool) bool {
	t.Helper()
	found := false
	driver := run["tool"].(map[string]interface{})["driver"].(map[string]interface{})
	if driver["name"] != "Scorecard" || driver["semanticVersion"] != "v5.5.0" {
		t.Fatalf("changed tool identity: %v", driver)
	}
	id := run["automationDetails"].(map[string]interface{})["id"].(string)
	parts := strings.SplitN(id, "/", 3)
	if len(parts) != 3 {
		t.Fatalf("invalid automation identity %q", id)
	}
	category := strings.Join(parts[:2], "/")
	if present, ok := categories[category]; !ok || present {
		t.Fatalf("unexpected or duplicate category %q", category)
	}
	categories[category] = true
	for _, rawFinding := range run["results"].([]interface{}) {
		f := rawFinding.(map[string]interface{})
		if f["ruleId"] != "PinnedDependenciesID" {
			t.Fatalf("unexpected finding %v", f)
		}
		location := f["locations"].([]interface{})[0].(map[string]interface{})["physicalLocation"].(map[string]interface{})
		if location["artifactLocation"].(map[string]interface{})["uri"] != ".github/workflows/build.yml" || location["region"].(map[string]interface{})["startLine"] != float64(7) {
			t.Fatalf("lost finding location: %v", location)
		}
		found = true
	}
	return found
}

func assertOfficialSARIFFindings(t *testing.T, sarif map[string]interface{}, failing bool) {
	t.Helper()
	found := false
	categories := map[string]bool{"supply-chain/local": false, "supply-chain/online-scm": false, "supply-chain/branch-protection": false}
	for _, raw := range sarif["runs"].([]interface{}) {
		runFound := assertOfficialSARIFRun(t, raw.(map[string]interface{}), categories)
		found = found || runFound
	}
	for category, present := range categories {
		if !present {
			t.Errorf("lost historical category %s", category)
		}
	}
	if found != failing {
		t.Fatalf("lost finding: found=%v want=%v", found, failing)
	}
}

func TestOfficialSARIFPreservesFindingsAndHistoricalIdentity(t *testing.T) {
	for _, failing := range []bool{false, true} {
		result := officialSARIFResult(t, failing)
		output := filepath.Join(t.TempDir(), "results.sarif")
		calls := 0
		if err := scanAndWrite(context.Background(), output, strings.Repeat("a", 40), func(context.Context) (scorecard.Result, error) {
			calls++
			return result, nil
		}); err != nil {
			t.Fatal(err)
		}
		if calls != 1 {
			t.Fatalf("scanner called %d times", calls)
		}
		data, err := os.ReadFile(output)
		if err != nil {
			t.Fatal(err)
		}
		var sarif map[string]interface{}
		if err := json.Unmarshal(data, &sarif); err != nil {
			t.Fatal(err)
		}
		assertOfficialSARIFFindings(t, sarif, failing)
	}
}

func TestScannerIgnoringContextCannotPublishAfterDeadline(t *testing.T) {
	output := filepath.Join(t.TempDir(), "results.sarif")
	if err := os.WriteFile(output, []byte("stale"), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	release := make(chan struct{})
	finished := make(chan struct{})
	defer func() {
		close(release)
		<-finished
		if _, err := os.Stat(output); !errors.Is(err, os.ErrNotExist) {
			t.Errorf("late scan result wrote SARIF: %v", err)
		}
	}()
	done := make(chan error, 1)
	go func() {
		done <- scanAndWrite(ctx, output, strings.Repeat("a", 40), func(context.Context) (scorecard.Result, error) {
			defer close(finished)
			<-release // Simulate upstream sleeping without observing the context.
			return completeResult(), nil
		})
	}()
	select {
	case err := <-done:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("expected deadline failure, got %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("scanner ignored the deadline")
	}
	if _, err := os.Stat(output); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("timed-out scan left SARIF: %v", err)
	}
}

func upstreamPackagingAbsent(t *testing.T) checker.CheckResult {
	t.Helper()
	findings, _, err := packagedWithAutomatedWorkflow.Run(&checker.RawResults{})
	if err != nil {
		t.Fatal(err)
	}
	logger := checker.NewLogger()
	result := evaluation.Packaging("Packaging", findings, logger)
	result.Findings, result.Details = findings, logger.Flush()
	return result
}

func TestPackagingAbsenceExceptionRejectsErrorsAndUnknownResults(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*checker.CheckResult)
	}{
		{"runtime error", func(c *checker.CheckResult) { c.Error = errors.New("API unavailable") }},
		{"different reason", func(c *checker.CheckResult) { c.Reason += " because API failed" }},
		{"different check", func(c *checker.CheckResult) { c.Name = "Binary-Artifacts" }},
		{"different version", func(c *checker.CheckResult) { c.Version = 3 }},
		{"invalid score", func(c *checker.CheckResult) { c.Score = -2 }},
		{"missing probe", func(c *checker.CheckResult) { c.Findings = nil }},
		{"duplicate probe", func(c *checker.CheckResult) { c.Findings = append(c.Findings, c.Findings[0]) }},
		{"different probe", func(c *checker.CheckResult) { c.Findings[0].Probe = "unknown" }},
		{"contradictory outcome", func(c *checker.CheckResult) { c.Findings[0].Outcome = finding.OutcomeTrue }},
	}
	if !packagingNotApplicable(upstreamPackagingAbsent(t)) {
		t.Fatal("real upstream absence result rejected")
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			c := upstreamPackagingAbsent(t)
			tt.mutate(&c)
			if packagingNotApplicable(c) {
				t.Fatal("invalid absence result accepted")
			}
		})
	}
}

func TestOfficialJSONPreservesPackagingAbsenceReason(t *testing.T) {
	result := completeResult()
	for i := range result.Checks {
		if result.Checks[i].Name == "Packaging" {
			result.Checks[i] = upstreamPackagingAbsent(t)
		}
	}
	docs, err := checkdocs.Read()
	if err != nil {
		t.Fatal(err)
	}
	var data bytes.Buffer
	if err := result.AsJSON2(&data, docs, nil); err != nil {
		t.Fatal(err)
	}
	var output struct {
		Checks []struct {
			Name   string
			Score  int
			Reason string
		}
	}
	if err := json.Unmarshal(data.Bytes(), &output); err != nil {
		t.Fatal(err)
	}
	for _, check := range output.Checks {
		if check.Name == "Packaging" {
			if check.Score != -1 || check.Reason != "packaging workflow not detected" {
				t.Fatalf("changed upstream JSON contract: %+v", check)
			}
			return
		}
	}
	t.Fatal("Packaging absent from JSON")
}
