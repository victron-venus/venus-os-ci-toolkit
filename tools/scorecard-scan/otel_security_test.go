package main

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// GHSA-q7gw-24fr-5p69: valid replacement runes must consume the configured limit.
func TestTelemetryAttributeLimitsCountReplacementRunes(t *testing.T) {
	const limit = 5
	cases := map[string]string{
		"replacement only": strings.Repeat("\uFFFD", 1000),
		"mixed":            strings.Repeat("\uFFFD", 100) + "ABCDEF",
		"interleaved":      "a\uFFFDb\uFFFDcd",
		"ASCII control":    "abcdefghij",
		"Unicode control":  "αβγδεζηθ",
	}
	for name, input := range cases {
		t.Run(name, func(t *testing.T) {
			recorder := tracetest.NewSpanRecorder()
			provider := sdktrace.NewTracerProvider(
				sdktrace.WithSpanProcessor(recorder),
				sdktrace.WithSpanLimits(sdktrace.SpanLimits{
					AttributeValueLengthLimit:   limit,
					AttributeCountLimit:         128,
					EventCountLimit:             128,
					LinkCountLimit:              128,
					AttributePerEventCountLimit: 128,
					AttributePerLinkCountLimit:  128,
				}),
			)
			t.Cleanup(func() {
				if err := provider.Shutdown(context.Background()); err != nil {
					t.Error(err)
				}
			})
			_, span := provider.Tracer("security-regression").Start(context.Background(), "bounded-attribute")
			span.SetAttributes(attribute.String("payload", input))
			span.End()
			ended := recorder.Ended()
			if len(ended) != 1 {
				t.Fatalf("expected one ended span, got %d", len(ended))
			}
			attrs := ended[0].Attributes()
			if len(attrs) != 1 || attrs[0].Key != "payload" {
				t.Fatal("expected the configured payload attribute")
			}
			want := string([]rune(input)[:limit])
			if got := attrs[0].Value.AsString(); got != want {
				t.Fatalf("attribute retained %d runes; expected the first %d runes", len([]rune(got)), limit)
			}
		})
	}
}
