# raigolmi-registry

The shared catalog for [RaiGolmi](https://github.com/ChristianBlevens/RaiGolmi): faces
(desktops), bodies (projects) and toolbelts (tool sets) that people built on their machines
and shared. RaiGolmi's catalog window lists what's here, downloads it and installs it. You
don't need to clone this repo.

## How it works

- `faces/`, `bodies/` and `toolbelts/` hold one folder per entry, `<kind>/<id>/`, with that
  kind's toml and the files its build reads.
- Uploading from RaiGolmi's catalog opens a pull request from your GitHub account. A check
  runs on it (one entry, its toml present, the same author as before when the entry already
  exists), and **a person merges it**: nothing is published on its own.
- Once merged, the `publish` workflow packs each changed entry into a release asset and
  rebuilds `index.json`, which names each entry's author (the login whose pull request first
  added it), version, size, download count and sha256. RaiGolmi checks a download against
  that sha256 before unpacking it.

## Before you install something

A layer here is someone else's code. Installed, it builds and runs on your machine with the
same reach as a layer you wrote yourself, including the agents' access to your GitHub
sign-in. Read what it does first, and only install what you'd be comfortable running.

## Licenses

By opening an upload, you offer what it contains under the MIT License ([LICENSE](LICENSE)),
unless its `LAYER.md` names another license it is offered under. Only upload what you have the
right to share that way. This repo's own scripts and workflows are MIT too.
