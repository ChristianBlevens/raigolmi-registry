<!-- purpose: what the Sign-in Browser face is and what each of its files does
not-here: how a face is built in general (/guide/faces.md); the sign-in steps themselves (the AI terminal prints them)
shape: bounded
audited: 1200 2026-10-06
-->
# Sign-in Browser

A face that is only Firefox, filling the screen. It exists for a machine running RaiGolmi on
its own, with no other computer's browser beside it: the AI terminal's sign-ins (the Claude
Code token, GitHub, claude.ai) each need a browser, and this is one. Codes and addresses the
terminal puts on the clipboard paste here with ctrl+v, and what a page gives back pastes into
the terminal with a right-click. Once signed in, ask the machine tab for the desktop you want.

- `face.toml`: a sway desktop whose one app is Firefox; no editor window.
- `desktop/sway.conf`: no borders, no gaps, no key bindings of its own; starts `desktop/start`.
- `desktop/start` (executable): waits for the apps, writes the profile's prefs (no first-run
  pages, no password prompts, blank new tabs, the last tabs reopened) and runs Firefox, again a
  second after it closes.
- `_compositors/sway/`: the compositor image, the same files every sway face carries.
