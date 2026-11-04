package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type observedBody struct {
	reader io.Reader
	read   int
	closed bool
}

func (b *observedBody) Read(p []byte) (int, error) {
	n, err := b.reader.Read(p)
	b.read += n
	return n, err
}
func (b *observedBody) Close() error { b.closed = true; return nil }

func noWait(context.Context, time.Duration) error { return nil }

func TestReadRetryTransientSuccessAndHeaderOwnership(t *testing.T) {
	request, _ := http.NewRequest(http.MethodGet, "https://api.github.com/repos/example/repo/statuses", nil)
	request.Header.Set("X-Caller", "retained")
	bodies := []*observedBody{}
	calls := 0
	delays := []time.Duration{}
	transport := &readRetryTransport{base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r == request || r.Header.Get("Authorization") != "" || r.Header.Get("X-Caller") != "retained" {
			t.Fatal("retry reused mutable request/header state")
		}
		r.Header.Add("Authorization", "synthetic")
		status := []int{502, 503, 200}[calls]
		calls++
		body := &observedBody{reader: strings.NewReader("payload")}
		bodies = append(bodies, body)
		return &http.Response{StatusCode: status, Body: body}, nil
	}), wait: func(_ context.Context, d time.Duration) error { delays = append(delays, d); return nil }}
	response, err := transport.RoundTrip(request)
	if err != nil || response.StatusCode != 200 || calls != 3 || len(delays) != 2 || delays[0] != time.Second || delays[1] != 2*time.Second {
		t.Fatalf("unexpected recovery: calls=%d delays=%v error=%v", calls, delays, err)
	}
	if request.Header.Get("Authorization") != "" || !bodies[0].closed || !bodies[1].closed || bodies[2].closed || bodies[2].read != 0 {
		t.Fatal("caller headers or response-body ownership changed")
	}
	defer response.Body.Close()
}

func TestReadRetryExhaustionPreservesFinalResponse(t *testing.T) {
	request, _ := http.NewRequest(http.MethodHead, "https://api.github.com/example", nil)
	calls := 0
	final := &http.Response{StatusCode: 504, Body: &observedBody{reader: strings.NewReader("final error")}}
	transport := &readRetryTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		if calls == 3 {
			return final, nil
		}
		return &http.Response{StatusCode: 504, Body: io.NopCloser(strings.NewReader("temporary"))}, nil
	}), wait: noWait}
	response, err := transport.RoundTrip(request)
	if err != nil || response != final || calls != 3 || final.Body.(*observedBody).closed || final.Body.(*observedBody).read != 0 {
		t.Fatal("exhaustion did not preserve the final failed response")
	}
	defer final.Body.Close()
}

func TestReadRetryNeverReplaysMutationsOrBody(t *testing.T) {
	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodGet} {
		t.Run(method, func(t *testing.T) {
			var body io.Reader
			if method == http.MethodGet {
				body = strings.NewReader("body")
			}
			request, _ := http.NewRequest(method, "https://api.github.com/example", body)
			calls := 0
			transport := &readRetryTransport{base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				calls++
				return &http.Response{StatusCode: 502, Body: http.NoBody}, nil
			}), wait: func(context.Context, time.Duration) error { t.Fatal("unexpected wait"); return nil }}
			response, err := transport.RoundTrip(request)
			if err != nil || calls != 1 || response.StatusCode != 502 {
				t.Fatal("unsafe replay")
			}
		})
	}
}

func TestReadRetryDoesNotReplayOtherStatuses(t *testing.T) {
	for _, status := range []int{200, 301, 400, 401, 403, 404, 408, 429, 500, 501} {
		request, _ := http.NewRequest(http.MethodGet, "https://api.github.com/example", nil)
		calls := 0
		body := &observedBody{reader: strings.NewReader("original")}
		transport := &readRetryTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) {
			calls++
			return &http.Response{StatusCode: status, Body: body}, nil
		}), wait: noWait}
		response, err := transport.RoundTrip(request)
		if err != nil || calls != 1 || response.StatusCode != status || body.closed || body.read != 0 {
			t.Fatalf("status %d was replayed or consumed", status)
		}
		response.Body.Close()
	}
}

func TestReadRetryPreservesTransportError(t *testing.T) {
	request, _ := http.NewRequest(http.MethodGet, "https://api.github.com/example", nil)
	want := errors.New("synthetic transport failure")
	calls := 0
	transport := &readRetryTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) { calls++; return nil, want }), wait: noWait}
	response, err := transport.RoundTrip(request)
	if response != nil || err != want || calls != 1 {
		t.Fatal("transport error changed or replayed")
	}
}

func TestReadRetryCancellationBeforeRequest(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.github.com/example", nil)
	transport := &readRetryTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) { t.Fatal("request after cancellation"); return nil, nil })}
	if _, err := transport.RoundTrip(request); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestReadRetryCancellationDuringBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.github.com/example", nil)
	body := &observedBody{reader: strings.NewReader("temporary")}
	calls := 0
	transport := &readRetryTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		return &http.Response{StatusCode: 503, Body: body}, nil
	}),
		wait: func(ctx context.Context, d time.Duration) error { cancel(); return waitReadRetry(ctx, d) }}
	if _, err := transport.RoundTrip(request); !errors.Is(err, context.Canceled) || calls != 1 || !body.closed {
		t.Fatal("cancellation lost or body retained", err)
	}
}

func TestReadRetryBoundsDiscardedBody(t *testing.T) {
	request, _ := http.NewRequest(http.MethodGet, "https://api.github.com/example", nil)
	body := &observedBody{reader: strings.NewReader(strings.Repeat("x", 256*1024))}
	calls := 0
	transport := &readRetryTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		if calls == 1 {
			return &http.Response{StatusCode: 502, Body: body}, nil
		}
		return &http.Response{StatusCode: 200, Body: http.NoBody}, nil
	}), wait: noWait}
	if _, err := transport.RoundTrip(request); err != nil || calls != 2 || body.read != 64*1024 || !body.closed {
		t.Fatal("error body not bounded/closed", err)
	}
}
