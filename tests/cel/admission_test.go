package admission_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os/exec"
	"testing"

	"cel.dev/cel-go/cel"
	"cel.dev/cel-go/common/types"
)

type expression struct {
	Name       string `json:"name"`
	Expression string `json:"expression"`
	Message    string `json:"message"`
}

type suite struct {
	Name   string `json:"name"`
	Policy struct {
		MatchConstraints struct {
			ResourceRules []struct {
				Operations []string `json:"operations"`
			} `json:"resourceRules"`
		} `json:"matchConstraints"`
		MatchConditions []expression `json:"matchConditions"`
		Variables       []expression `json:"variables"`
		Validations     []expression `json:"validations"`
	} `json:"policy"`
	Cases []struct {
		Name    string         `json:"name"`
		Context map[string]any `json:"context"`
		Allow   bool           `json:"allow"`
	} `json:"cases"`
}

func numbers(value any) any {
	switch value := value.(type) {
	case json.Number:
		n, err := value.Int64()
		if err != nil {
			panic(err) // fixtures contain only Kubernetes integer fields
		}
		return n
	case map[string]any:
		for k, v := range value {
			value[k] = numbers(v)
		}
	case []any:
		for i, v := range value {
			value[i] = numbers(v)
		}
	}
	return value
}

// These are real CEL evaluations of the emitted expressions on dynamic Pod
// fixtures. Kubernetes schema typing, defaulting, authorization and admission
// integration still require a server-side dry run before applying a plan.
func TestRenderedAdmission(t *testing.T) {
	output, err := exec.Command("python3", "../test_runner_qualification.py", "--cel-fixtures").CombinedOutput()
	if err != nil {
		t.Fatalf("render fixtures: %v\n%s", err, output)
	}
	decoder := json.NewDecoder(bytes.NewReader(output))
	decoder.UseNumber()
	var suites []suite
	if err := decoder.Decode(&suites); err != nil {
		t.Fatal(err)
	}
	env, err := cel.NewEnv(cel.Variable("object", cel.DynType), cel.Variable("oldObject", cel.DynType),
		cel.Variable("request", cel.DynType), cel.Variable("variables", cel.DynType))
	if err != nil {
		t.Fatal(err)
	}
	compile := func(items []expression) []cel.Program {
		result := make([]cel.Program, 0, len(items))
		for _, item := range items {
			ast, issues := env.Compile(item.Expression)
			if issues.Err() != nil {
				t.Fatalf("CEL compilation must never count as denial: %s: %v", item.Message, issues.Err())
			}
			program, err := env.Program(ast, cel.CostLimit(1000000))
			if err != nil {
				t.Fatal(err)
			}
			result = append(result, program)
		}
		return result
	}
	for _, s := range suites {
		t.Run(s.Name, func(t *testing.T) {
			conditions := compile(s.Policy.MatchConditions)
			variables := compile(s.Policy.Variables)
			validations := compile(s.Policy.Validations)
			for _, c := range s.Cases {
				t.Run(c.Name, func(t *testing.T) {
					activation := numbers(c.Context).(map[string]any)
					activation["variables"] = map[string]any{}
					operation := activation["request"].(map[string]any)["operation"]
					matched := false
					for _, rule := range s.Policy.MatchConstraints.ResourceRules {
						for _, op := range rule.Operations {
							matched = matched || op == operation || op == "*"
						}
					}
					allowed, reason := true, "outside policy operations"
					if matched {
						for _, p := range conditions {
							v, _, e := p.Eval(activation)
							if e != nil || types.IsError(v) {
								allowed, reason = false, fmt.Sprintf("match failure: %v / %v", e, v)
								break
							}
							matched = matched && v == types.True
						}
					}
					if matched && allowed {
						for i, p := range variables {
							v, _, e := p.Eval(activation)
							if e != nil || types.IsError(v) {
								allowed, reason = false, fmt.Sprintf("variable failure: %v / %v", e, v)
								break
							}
							activation["variables"].(map[string]any)[s.Policy.Variables[i].Name] = v
						}
					}
					if matched && allowed {
						for i, p := range validations {
							v, _, e := p.Eval(activation)
							if e != nil || v != types.True {
								allowed, reason = false, fmt.Sprintf("%s: %v / %v", s.Policy.Validations[i].Message, e, v)
								break
							}
						}
					}
					if allowed != c.Allow {
						t.Fatalf("allowed=%v want=%v: %s", allowed, c.Allow, reason)
					}
				})
			}
		})
	}
}
