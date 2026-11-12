package main

import (
	"context"
	"crypto/fips140"
	"crypto/tls"
	"errors"
	"io"
	"net/http"
	"runtime"
	"strings"
	"testing"
	"time"
)

type testTransport func(*http.Request) (*http.Response, error)

func (f testTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type testBody struct {
	reader io.Reader
	closed bool
}

func (b *testBody) Read(p []byte) (int, error) { return b.reader.Read(p) }
func (b *testBody) Close() error               { b.closed = true; return nil }

type errorReader struct{ err error }

func (r errorReader) Read([]byte) (int, error) { return 0, r.err }

type repeatingReader struct{ read int }

func (r *repeatingReader) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = 'x'
	}
	r.read += len(p)
	return len(p), nil
}

type cancelReader struct{ cancel context.CancelFunc }

func (r cancelReader) Read([]byte) (int, error) { r.cancel(); return 0, io.EOF }

func response(status int, body *testBody) *http.Response {
	return &http.Response{StatusCode: status, Header: make(http.Header), Body: body, ContentLength: -1}
}

func TestQualifiedRuntime(t *testing.T) {
	if runtime.Version() != "go1.27.4" || !fips140.Enabled() {
		t.Fatal("regressions must run with the production Go 1.27.4 FIPS profile")
	}
}

func TestImmediateSuccessUsesOneRequestWithoutWaiting(t *testing.T) {
	calls := 0
	body := &testBody{reader: strings.NewReader("complete archive bytes")}
	client := &http.Client{Transport: testTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Method != http.MethodGet {
			t.Fatal("unexpected request method")
		}
		return response(http.StatusOK, body), nil
	})}
	data, err := fetchArchive(context.Background(), client, "https://example.invalid/archive", func(context.Context, time.Duration) error {
		t.Fatal("successful first request must not wait")
		return nil
	})
	if err != nil || string(data) != "complete archive bytes" || calls != 1 || !body.closed {
		t.Fatalf("calls=%d closed=%v data=%q error=%v", calls, body.closed, data, err)
	}
}

func TestEachTransientStatusThenSuccess(t *testing.T) {
	for _, status := range []int{429, 500, 502, 503, 504} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			checkTransientThenSuccess(t, status)
		})
	}
}

func checkTransientThenSuccess(t *testing.T, status int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	deadline, _ := ctx.Deadline()
	calls, waits := 0, 0
	first := &testBody{reader: strings.NewReader("transient error")}
	last := &testBody{reader: strings.NewReader("verified later by installer")}
	client := &http.Client{Transport: testTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		got, ok := r.Context().Deadline()
		if !ok || !got.Equal(deadline) || r.Method != http.MethodGet {
			t.Fatal("attempt changed deadline or method")
		}
		if calls == 1 {
			return response(status, first), nil
		}
		return response(200, last), nil
	})}
	data, err := fetchArchive(ctx, client, "https://example.invalid/archive", func(got context.Context, delay time.Duration) error {
		waits++
		if !first.closed || got != ctx || delay != time.Second {
			t.Fatal("body not closed before bounded retry")
		}
		return nil
	})
	if err != nil || string(data) != "verified later by installer" || calls != 2 || waits != 1 || !last.closed {
		t.Fatalf("calls=%d waits=%d closed=%v error=%v", calls, waits, last.closed, err)
	}
}

func TestTransientExhaustionHasExactlyThreeAttempts(t *testing.T) {
	calls := 0
	var waits []time.Duration
	var bodies []*testBody
	client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) {
		calls++
		body := &testBody{reader: strings.NewReader("unavailable")}
		bodies = append(bodies, body)
		return response(504, body), nil
	})}
	data, err := fetchArchive(context.Background(), client, "https://example.invalid/archive", func(_ context.Context, delay time.Duration) error {
		waits = append(waits, delay)
		return nil
	})
	if err == nil || !strings.Contains(err.Error(), "HTTP 504") || data != nil || calls != 3 || len(waits) != 2 || waits[0] != time.Second || waits[1] != 2*time.Second {
		t.Fatalf("calls=%d waits=%v data=%v error=%v", calls, waits, data, err)
	}
	for _, body := range bodies {
		if !body.closed {
			t.Fatal("response body leaked")
		}
	}
}

func TestOtherStatusesAreNeverRetried(t *testing.T) {
	for _, status := range []int{400, 401, 403, 404, 408, 409, 501, 505} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			calls := 0
			body := &testBody{reader: strings.NewReader("terminal error")}
			client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) {
				calls++
				return response(status, body), nil
			})}
			_, err := fetchArchive(context.Background(), client, "https://example.invalid/archive", func(context.Context, time.Duration) error {
				t.Fatal("non-transient status retried")
				return nil
			})
			if err == nil || calls != 1 || !body.closed {
				t.Fatalf("calls=%d error=%v", calls, err)
			}
		})
	}
}

func TestTransportAndTLSErrorsAreNeverRetried(t *testing.T) {
	for _, failure := range []error{errors.New("connection reset"), tls.RecordHeaderError{Msg: "invalid TLS record"}, context.DeadlineExceeded} {
		calls := 0
		client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) { calls++; return nil, failure })}
		_, err := fetchArchive(context.Background(), client, "https://example.invalid/archive", func(context.Context, time.Duration) error {
			t.Fatal("transport error retried")
			return nil
		})
		if !errors.Is(err, failure) || calls != 1 {
			t.Fatalf("calls=%d error=%v", calls, err)
		}
	}
}

func TestBodyFailureAfterRetryDoesNotRestartDownload(t *testing.T) {
	failure := errors.New("truncated body")
	calls := 0
	body := &testBody{reader: errorReader{failure}}
	client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) {
		calls++
		if calls == 1 {
			return response(504, &testBody{reader: strings.NewReader("")}), nil
		}
		return response(200, body), nil
	})}
	data, err := fetchArchive(context.Background(), client, "https://example.invalid/archive", func(context.Context, time.Duration) error { return nil })
	if !errors.Is(err, failure) || data != nil || calls != 2 || !body.closed {
		t.Fatalf("calls=%d error=%v", calls, err)
	}
}

func TestAdvertisedAndStreamedSizeLimitsAreTerminal(t *testing.T) {
	for _, advertised := range []bool{true, false} {
		calls := 0
		reader := &repeatingReader{}
		body := &testBody{reader: reader}
		client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) {
			calls++
			r := response(200, body)
			if advertised {
				r.ContentLength = maxArchive + 1
			}
			return r, nil
		})}
		data, err := fetchArchive(context.Background(), client, "https://example.invalid/archive", func(context.Context, time.Duration) error {
			t.Fatal("oversized archive retried")
			return nil
		})
		if err == nil || !strings.Contains(err.Error(), "size limit") || data != nil || calls != 1 || !body.closed {
			t.Fatalf("error=%v calls=%d", err, calls)
		}
		want := maxArchive + 1
		if advertised {
			want = 0
		}
		if reader.read != want {
			t.Fatalf("read=%d want=%d", reader.read, want)
		}
	}
}

func TestCanceledBeforeFirstRequestDoesNotSend(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) { t.Fatal("request after cancellation"); return nil, nil })}
	_, err := fetchArchive(ctx, client, "https://example.invalid/archive", waitForRetry)
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestCancellationDuringWaitPreventsNextAttempt(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	calls := 0
	client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) {
		calls++
		return response(503, &testBody{reader: strings.NewReader("")}), nil
	})}
	_, err := fetchArchive(ctx, client, "https://example.invalid/archive", func(ctx context.Context, delay time.Duration) error {
		cancel()
		return waitForRetry(ctx, delay)
	})
	if !errors.Is(err, context.Canceled) || calls != 1 {
		t.Fatalf("calls=%d error=%v", calls, err)
	}
}

func TestLateSuccessIsRejected(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	body := &testBody{reader: cancelReader{cancel}}
	client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) { return response(200, body), nil })}
	data, err := fetchArchive(ctx, client, "https://example.invalid/archive", waitForRetry)
	if !errors.Is(err, context.Canceled) || data != nil || !body.closed {
		t.Fatalf("data=%v error=%v", data, err)
	}
}

func TestRetryWaitCannotExtendDeadline(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if err := waitForRetry(ctx, 61*time.Second); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	if err := ctx.Err(); err != nil {
		t.Fatalf("wait consumed the budget instead of rejecting early: %v", err)
	}
}

func TestRetryAfterHonorsLongerDelayAndRejectsMalformedValue(t *testing.T) {
	now := time.Date(2026, 11, 5, 14, 0, 0, 0, time.UTC)
	for _, test := range []struct {
		header string
		want   time.Duration
		bad    bool
	}{
		{"", time.Second, false}, {"0", time.Second, false}, {"5", 5 * time.Second, false},
		{now.Add(10 * time.Second).Format(http.TimeFormat), 10 * time.Second, false},
		{now.Add(-10 * time.Second).Format(http.TimeFormat), time.Second, false},
		{"61", 0, true}, {"999999999999999999999", 0, true}, {"-1", 0, true}, {"bad", 0, true},
	} {
		got, err := retryDelay(test.header, now, time.Second)
		if (err != nil) != test.bad || (!test.bad && got != test.want) {
			t.Fatalf("header=%q delay=%v error=%v", test.header, got, err)
		}
	}
}

func TestRetryAfterBeyondBudgetDoesNotSendAgain(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	calls := 0
	client := &http.Client{Transport: testTransport(func(*http.Request) (*http.Response, error) {
		calls++
		r := response(429, &testBody{reader: strings.NewReader("")})
		r.Header.Set("Retry-After", "60")
		return r, nil
	})}
	_, err := fetchArchive(ctx, client, "https://example.invalid/archive", waitForRetry)
	if !errors.Is(err, context.DeadlineExceeded) || calls != 1 {
		t.Fatalf("calls=%d error=%v", calls, err)
	}
}
