// scorecard-scan refuses to upload incomplete scans, using upstream SARIF output.
package main

import (
	"context"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/ossf/scorecard/v5/checker"
	"github.com/ossf/scorecard/v5/clients/githubrepo"
	"github.com/ossf/scorecard/v5/docs/checks"
	"github.com/ossf/scorecard/v5/log"
	"github.com/ossf/scorecard/v5/options"
	"github.com/ossf/scorecard/v5/pkg/scorecard"
	"github.com/ossf/scorecard/v5/policy"
)

// Preserve the reporting policy shipped by ossf/scorecard-action v2.4.4:
// https://github.com/ossf/scorecard-action/blob/55891bbd73f2425e97637d96e306fc9d491d0b21/policies/template.yml
// Negative thresholds denote its intentionally disabled checks, not a scan failure.
var thresholds = map[string]int{
	"Token-Permissions": 10, "Branch-Protection": 10, "Code-Review": 10,
	"Dangerous-Workflow": 10, "License": 9, "Pinned-Dependencies": 10,
	"Security-Policy": 10, "SAST": 10, "Contributors": -1, "Packaging": 10,
	"Binary-Artifacts": 10, "Signed-Releases": -1, "Dependency-Update-Tool": 10,
	"Fuzzing": 10, "CII-Best-Practices": 5, "Vulnerabilities": 10,
	"CI-Tests": 10, "Maintained": 1,
}

func reportingPolicy() (*policy.ScorecardPolicy, []string) {
	p := &policy.ScorecardPolicy{Version: 1, Policies: map[string]*policy.CheckPolicy{}}
	var names []string
	for name, threshold := range thresholds {
		entry := &policy.CheckPolicy{Score: int32(threshold), Mode: policy.CheckPolicy_ENFORCED}
		if threshold < 0 {
			entry.Score, entry.Mode = 10, policy.CheckPolicy_DISABLED
		}
		p.Policies[name] = entry
		names = append(names, name)
	}
	sort.Strings(names)
	return p, names
}

func validate(result *scorecard.Result) error {
	seen := make(map[string]bool)
	var problems []error
	for _, check := range result.Checks {
		threshold, expected := thresholds[check.Name]
		if !expected || seen[check.Name] {
			problems = append(problems, fmt.Errorf("unexpected or duplicate Scorecard check %q", check.Name))
			continue
		}
		seen[check.Name] = true
		if threshold >= 0 && (check.Error != nil || check.Score < checker.MinResultScore || check.Score > checker.MaxResultScore) {
			problems = append(problems, fmt.Errorf("incomplete Scorecard check %q: score=%d, runtime error=%v", check.Name, check.Score, check.Error))
		}
	}
	for name := range thresholds {
		if !seen[name] {
			problems = append(problems, fmt.Errorf("missing Scorecard check %q", name))
		}
	}
	return errors.Join(problems...)
}

// Use exactly one Scan Result for validation and formatting. A failed rerun must
// remove stale output; neither a partial scan nor a formatter error can publish.
func scanAndWrite(ctx context.Context, output, commit string, scan func(context.Context) (scorecard.Result, error)) error {
	if err := os.Remove(output); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	// Upstream rate-limit sleeps may ignore cancellation. Only the scan runs in
	// the background; a late result cannot format or publish SARIF after timeout.
	type scanResult struct {
		result scorecard.Result
		err    error
	}
	done := make(chan scanResult, 1)
	go func() {
		result, err := scan(ctx)
		done <- scanResult{result, err}
	}()
	var scanned scanResult
	select {
	case <-ctx.Done():
		return fmt.Errorf("Scorecard scan deadline: %w", ctx.Err())
	case scanned = <-done:
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("Scorecard scan deadline: %w", err)
	}
	result, err := scanned.result, scanned.err
	if err != nil {
		return fmt.Errorf("Scorecard scan failed: %w", err)
	}
	if result.Repo.CommitSHA != commit {
		return fmt.Errorf("scanned commit %q does not match expected commit %q", result.Repo.CommitSHA, commit)
	}
	if err := validate(&result); err != nil {
		return err
	}
	docs, err := checks.Read()
	if err != nil {
		return err
	}
	// The library's version globals describe this wrapper unless explicitly set.
	// These identify the immutable upstream module actually used by this binary.
	result.Scorecard = scorecard.ScorecardInfo{Version: "v5.5.0", CommitSHA: "c395761df6afe1a69e476bc60a013a94bcbc153f"}
	p, _ := reportingPolicy()
	file, err := os.CreateTemp(filepath.Dir(output), ".scorecard-*.sarif")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	err = result.AsSARIF(true, log.DefaultLevel, file, docs, p, &options.Options{})
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(file.Name(), output)
}

func main() {
	repoName := flag.String("repo", "", "GitHub owner/repository to scan")
	commit := flag.String("commit", "", "expected full GitHub commit SHA (required)")
	output := flag.String("output", "results.sarif", "SARIF output (removed when a scan fails)")
	flag.Parse()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	err := scanAndWrite(ctx, *output, *commit, func(ctx context.Context) (scorecard.Result, error) {
		if decoded, err := hex.DecodeString(*commit); err != nil || len(decoded) != 20 {
			return scorecard.Result{}, errors.New("-commit must be a full 40-character hexadecimal commit SHA")
		}
		repo, err := githubrepo.MakeGithubRepo(*repoName)
		if err != nil {
			return scorecard.Result{}, err
		}
		_, names := reportingPolicy()
		// GitHub branch/release metadata checks only support HEAD upstream.
		// scanAndWrite rejects HEAD if it moved beyond the workflow commit.
		return scorecard.Run(ctx, repo, scorecard.WithChecks(names), scorecard.WithFileModeGit())
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
