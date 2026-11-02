package main

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	transitiverequirements "github.com/google/osv-scalibr/enricher/transitivedependency/requirements"
	"github.com/google/osv-scanner/v2/pkg/models"
	"github.com/google/osv-scanner/v2/pkg/osvscanner"
	"github.com/ossf/osv-schema/bindings/go/osvschema"
	"github.com/ossf/scorecard/v5/clients"
)

func TestRequirementsScanUsesDeclaredInventory(t *testing.T) {
	client := requirementsOSVClient{scan: func(actions osvscanner.ScannerActions) (models.VulnerabilityResults, error) {
		if !reflect.DeepEqual(actions.DirectoryPaths, []string{"/checked/source"}) ||
			!reflect.DeepEqual(actions.GitCommits, []string{"commit"}) || !actions.Recursive || actions.IncludeGitRoot {
			t.Fatalf("changed source scan scope: %+v", actions)
		}
		if !reflect.DeepEqual(actions.PluginsEnabled, []string{"python/requirements"}) ||
			!reflect.DeepEqual(actions.PluginsDisabled, []string{transitiverequirements.Name}) {
			t.Fatalf("requirements extractor must remain active without online resolution: %+v", actions)
		}
		if actions.CompareOffline || actions.DownloadDatabases {
			t.Fatal("changed the default online advisory lookup")
		}
		return models.VulnerabilityResults{}, nil
	}}
	if _, err := client.ListUnfixedVulnerabilities(context.Background(), "commit", "/checked/source"); err != nil {
		t.Fatal(err)
	}
	actions := requirementsScanActions("", "")
	if len(actions.DirectoryPaths) != 0 || len(actions.GitCommits) != 0 {
		t.Fatalf("empty inputs must not add scan roots: %+v", actions)
	}
}

func fixturePackage(ecosystem, name, id string) models.PackageVulns {
	return models.PackageVulns{
		Package:         models.PackageInfo{Ecosystem: ecosystem, Name: name, Version: "1.0.0"},
		Vulnerabilities: []*osvschema.Vulnerability{{Id: id, Aliases: []string{"CVE-test"}}},
		Groups:          []models.GroupInfo{{IDs: []string{id}}},
	}
}

func TestRequirementsClientPreservesActualFindings(t *testing.T) {
	result := models.VulnerabilityResults{Results: []models.PackageSource{{Packages: []models.PackageVulns{
		fixturePackage("PyPI", "certifi", "PYSEC-test"),
		fixturePackage("PyPI", "certifi", "PYSEC-test"),
		fixturePackage("Go", "golang.org/x/net", "GO-package-test"),
		fixturePackage("Go", "stdlib", "GO-stdlib-test"),
		fixturePackage("PyPI", "stdlib", "PYSEC-stdlib-test"),
	}}}}
	client := requirementsOSVClient{scan: func(osvscanner.ScannerActions) (models.VulnerabilityResults, error) {
		return result, osvscanner.ErrVulnerabilitiesFound
	}}
	got, err := client.ListUnfixedVulnerabilities(context.Background(), "", "/checked/source")
	if err != nil {
		t.Fatal(err)
	}
	want := clients.VulnerabilitiesResponse{Vulnerabilities: []clients.Vulnerability{
		{ID: "PYSEC-test", Aliases: []string{"CVE-test"}},
		{ID: "GO-package-test", Aliases: []string{"CVE-test"}},
		{ID: "PYSEC-stdlib-test", Aliases: []string{"CVE-test"}},
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("changed upstream finding semantics: got %+v, want %+v", got, want)
	}
}

func TestRequirementsClientErrorsRemainErrors(t *testing.T) {
	want := errors.New("advisory service unavailable")
	client := requirementsOSVClient{scan: func(osvscanner.ScannerActions) (models.VulnerabilityResults, error) {
		return models.VulnerabilityResults{}, want
	}}
	if _, err := client.ListUnfixedVulnerabilities(context.Background(), "", ""); !errors.Is(err, want) {
		t.Fatalf("lost scanner error: %v", err)
	}
	client.scan = func(osvscanner.ScannerActions) (models.VulnerabilityResults, error) {
		panic("broken scanner")
	}
	if _, err := client.ListUnfixedVulnerabilities(context.Background(), "", ""); err == nil || !strings.Contains(err.Error(), "broken scanner") {
		t.Fatalf("panic became a clean scan: %v", err)
	}
	client.scan = func(osvscanner.ScannerActions) (models.VulnerabilityResults, error) {
		return models.VulnerabilityResults{}, osvscanner.ErrNoPackagesFound
	}
	if _, err := client.ListUnfixedVulnerabilities(context.Background(), "", ""); err != nil {
		t.Fatalf("changed upstream no-package behavior: %v", err)
	}
}
