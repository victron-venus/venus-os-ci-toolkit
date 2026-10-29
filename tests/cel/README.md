# Emitted admission CEL contracts

Run `go test -count=1 -mod=readonly ./...` from this directory with Go 1.27.3 and Python 3.
The test obtains public, synthetic Pod fixtures and the actual rendered admission
expressions from `../test_runner_qualification.py`. It compiles and evaluates them
with the locked CEL interpreter; there is no replacement admission predicate.

Compilation errors always fail the suite. Runtime errors fail closed, consistent
with the policy's `failurePolicy: Fail`. Positive cases must evaluate successfully.
The default production policy, both explicit probe profiles, operator restriction,
UPDATE identity preservation, DELETE cleanup, and malformed input denials are
covered. Evaluation has a bounded cost limit and needs no cluster or credentials.

The fixtures use dynamic JSON values. They do **not** simulate Kubernetes schema
typing, all API defaults, mutating admission, authorization, scheduling or runtime
behavior. Before a live policy change, inspect Kubernetes type-checking status and
perform the documented server-side dry runs. The CEL module is a test dependency;
it does not change the cluster's CEL interpreter or the Scorecard toolchain.
