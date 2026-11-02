// Copyright 2021 OpenSSF Scorecard Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	transitiverequirements "github.com/google/osv-scalibr/enricher/transitivedependency/requirements"
	"github.com/google/osv-scanner/v2/pkg/models"
	"github.com/google/osv-scanner/v2/pkg/osvscanner"
	"github.com/ossf/scorecard/v5/clients"
)

// Adapted from Scorecard v5.5.0 clients/osv.go. Keep its default online scan
// behavior, using the renamed enricher identifier from upstream commit
// 2ef237f4e967d18e600e71aa36ab3be640314650. This is injected through the public
// client interface; the Scorecard evaluator and SARIF formatter remain upstream.
type requirementsOSVClient struct {
	scan func(osvscanner.ScannerActions) (models.VulnerabilityResults, error)
}

var _ clients.VulnerabilitiesClient = requirementsOSVClient{}

func requirementsScanActions(commit, localPath string) osvscanner.ScannerActions {
	actions := osvscanner.ScannerActions{
		IncludeGitRoot: false,
		Recursive:      true,
		ExperimentalScannerActions: osvscanner.ExperimentalScannerActions{
			PluginsEnabled:  []string{"python/requirements"},
			PluginsDisabled: []string{transitiverequirements.Name},
		},
	}
	if localPath != "" {
		actions.DirectoryPaths = []string{localPath}
	}
	if commit != "" {
		actions.GitCommits = []string{commit}
	}
	return actions
}

func (v requirementsOSVClient) ListUnfixedVulnerabilities(_ context.Context, commit, localPath string) (response clients.VulnerabilitiesResponse, err error) {
	osvscanner.SetLogger(slog.DiscardHandler)
	defer func() {
		if panicValue := recover(); panicValue != nil {
			response = clients.VulnerabilitiesResponse{}
			err = fmt.Errorf("osv-scanner panic: %v", panicValue)
		}
	}()
	scan := v.scan
	if scan == nil {
		scan = osvscanner.DoScan
	}
	result, err := scan(requirementsScanActions(commit, localPath))
	if err == nil || errors.Is(err, osvscanner.ErrNoPackagesFound) {
		return response, nil
	}
	if !errors.Is(err, osvscanner.ErrVulnerabilitiesFound) {
		return response, fmt.Errorf("osvscanner.DoScan: %w", err)
	}
	seen := make(map[string]bool)
	for _, item := range result.Flatten() {
		// Preserve upstream's Go directive treatment. Other ecosystems and all
		// actual dependency findings are retained, including requirements files.
		if item.Package.Ecosystem == "Go" && item.Package.Name == "stdlib" {
			continue
		}
		id := item.Vulnerability.GetId()
		if !seen[id] {
			seen[id] = true
			response.Vulnerabilities = append(response.Vulnerabilities, clients.Vulnerability{
				ID: id, Aliases: item.Vulnerability.GetAliases(),
			})
		}
	}
	return response, nil
}
