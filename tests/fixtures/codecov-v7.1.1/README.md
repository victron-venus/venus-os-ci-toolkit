# Pinned Codecov integrity regression fixture

`codecov.sh` is the unmodified wrapper from
[codecov-action 303a32d7a59b442fa8d48b6a1cc6825c09c847a5](https://github.com/codecov/codecov-action/blob/303a32d7a59b442fa8d48b6a1cc6825c09c847a5/dist/codecov.sh),
the action pinned by the Python workflow. Its SHA256 is
`1603474143632611c1913295378b412ed9a8deceabcd8bbd4ccd71ce7ac77790`.
The upstream MIT license is included.

Tests run this exact wrapper with fake curl, GPG, checksum and sleep commands.
No network access or real downloaded executable is used. The fake uploader
leaves an execution marker, allowing the tests to prove that verification errors
prevent execution while a verified upload failure remains visible as failure.
Update the fixture, hash and action reference together when changing the pin.
