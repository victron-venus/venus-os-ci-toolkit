package main

import (
	"context"
	"io"
	"net/http"
	"time"
)

// Retry only body-free reads rejected by a transient gateway/service response.
// The scanner's original context bounds all attempts; errors and final responses
// pass through unchanged. Authentication/rate handling remains upstream-owned.
type readRetryTransport struct {
	base http.RoundTripper
	wait func(context.Context, time.Duration) error
}

func waitReadRetry(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func (transport *readRetryTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if (request.Method != http.MethodGet && request.Method != http.MethodHead) ||
		(request.Body != nil && request.Body != http.NoBody) {
		return transport.base.RoundTrip(request)
	}
	wait := transport.wait
	if wait == nil {
		wait = waitReadRetry
	}
	for attempt := 0; attempt < 3; attempt++ {
		if err := request.Context().Err(); err != nil {
			return nil, err
		}
		// Upstream adds Authorization to its request. A fresh header copy avoids
		// accumulating credentials across retries or mutating the caller's request.
		response, err := transport.base.RoundTrip(request.Clone(request.Context()))
		if err != nil || response == nil || attempt == 2 ||
			(response.StatusCode != http.StatusBadGateway &&
				response.StatusCode != http.StatusServiceUnavailable &&
				response.StatusCode != http.StatusGatewayTimeout) {
			return response, err
		}
		if response.Body != nil {
			// Bound consumption; closing abandons any larger error payload. The
			// HTTP body shares the request context, including its existing deadline.
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64*1024))
			_ = response.Body.Close()
		}
		if err := wait(request.Context(), time.Duration(attempt+1)*time.Second); err != nil {
			return nil, err
		}
	}
	panic("unreachable")
}
