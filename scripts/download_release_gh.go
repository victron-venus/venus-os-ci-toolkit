// Download a reviewed release artifact with the qualified Go FIPS TLS profile.
package main

import (
	"context"
	"crypto/fips140"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"time"
)

const maxArchive = 32 * 1024 * 1024

func download(address, output string) error {
	if runtime.Version() != "go1.27.4" || !fips140.Enabled() {
		return errors.New("downloader requires Go 1.27.4 and GODEBUG=fips140=on")
	}
	parsed, err := url.Parse(address)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil {
		return errors.New("download requires an HTTPS URL without credentials")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	client := &http.Client{
		Transport: transport,
		Timeout:   60 * time.Second,
		CheckRedirect: func(request *http.Request, via []*http.Request) error {
			if request.URL.Scheme != "https" || request.URL.User != nil || len(via) >= 10 {
				return errors.New("redirect must retain HTTPS and stay within ten hops")
			}
			return nil
		},
	}
	// One deadline covers every attempt, redirect, response body and retry wait.
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	data, err := fetchArchive(ctx, client, address, waitForRetry)
	if err != nil {
		return err
	}
	file, err := os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(data)
	closeErr := file.Close()
	if writeErr != nil {
		return writeErr
	}
	return closeErr
}

// fetchArchive retries only completed transient HTTP responses. Transport and
// body errors return immediately; checksum validation remains in the installer.
func fetchArchive(ctx context.Context, client *http.Client, address string, wait func(context.Context, time.Duration) error) ([]byte, error) {
	for attempt := 1; attempt <= 3; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
		if err != nil {
			return nil, err
		}
		response, err := client.Do(request)
		if err != nil {
			return nil, err
		}
		if response.StatusCode == http.StatusOK {
			return readArchive(ctx, response)
		}
		response.Body.Close()
		statusError := fmt.Errorf("download returned HTTP %d", response.StatusCode)
		switch response.StatusCode {
		case http.StatusTooManyRequests, http.StatusInternalServerError,
			http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			if attempt == 3 {
				return nil, statusError
			}
		default:
			return nil, statusError
		}
		delay, err := retryDelay(response.Header.Get("Retry-After"), time.Now(), time.Duration(attempt)*time.Second)
		if err != nil {
			return nil, fmt.Errorf("%w: %v", statusError, err)
		}
		if err := wait(ctx, delay); err != nil {
			return nil, fmt.Errorf("%w: %w", statusError, err)
		}
	}
	return nil, errors.New("download attempts exhausted")
}

// readArchive closes the successful response and checks its size and deadline.
func readArchive(ctx context.Context, response *http.Response) ([]byte, error) {
	defer response.Body.Close()
	if response.ContentLength > maxArchive {
		return nil, errors.New("archive exceeds size limit")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxArchive+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxArchive {
		return nil, errors.New("archive exceeds size limit")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return data, nil
}

func retryDelay(header string, now time.Time, minimum time.Duration) (time.Duration, error) {
	if header == "" {
		return minimum, nil
	}
	var delay time.Duration
	if seconds, err := strconv.ParseUint(header, 10, 32); err == nil {
		// Avoid duration overflow and waits that cannot fit the total HTTP budget.
		if seconds > 60 {
			return 0, errors.New("Retry-After exceeds download deadline")
		}
		delay = time.Duration(seconds) * time.Second
	} else {
		when, err := http.ParseTime(header)
		if err != nil {
			return 0, errors.New("invalid Retry-After")
		}
		delay = when.Sub(now)
	}
	if delay > minimum {
		return delay, nil
	}
	return minimum, nil
}

func waitForRetry(ctx context.Context, delay time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) <= delay {
		return context.DeadlineExceeded
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return ctx.Err()
	}
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: download_release_gh HTTPS_URL NEW_OUTPUT_FILE")
		os.Exit(2)
	}
	if err := download(os.Args[1], os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
