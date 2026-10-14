# adidiodev fork of haomnilogic-local

This fork tracks [cryptk/haomnilogic-local](https://github.com/cryptk/haomnilogic-local) and adds a few changes on top, developed against a two-heater OmniPL (HDF400 gas + HP31405T heat/cool heat pump, TCELLS340, CSAD). Upstream is welcome to adopt any of it.

## Added on top of upstream

| Change | Files | Status upstream |
|---|---|---|
| Climate entity for virtual heaters: Heat / Cool / Auto / Off for cooling-capable heat pumps, per-unit attributes | `climate.py`, one line in `__init__.py` `PLATFORMS` | Not proposed yet |
| Chlorinator cell diagnostics: cell temp, cell voltage, cell current, board temp (`GetCHLORMeasurement`), polled every 60 s while the body of water has flow | `chlorinator_diagnostics.py`, hook at end of `sensor.async_setup_entry` | Library equivalent is open as python-omnilogic-local PR #154; switch to it once merged |
| Grace polls + stuck-entity fix (cherry-picked from kfriedenberger, PR #272) | `const.py`, `coordinator.py`, `entity.py` | Open as PR #272; drop the commit once merged |

## Releases

Tags are `<upstream version>.<fork revision>` (e.g. `2.0.2.1`). Each GitHub release carries an `omnilogic_local.zip` asset because `hacs.json` uses `zip_release`. HACS installs from these releases.

## Updating to a new upstream release

```sh
git fetch upstream --tags
git rebase <new upstream tag>        # replay the fork commits
# drop any fork commit upstream has merged (e.g. PR #272)
git push --force-with-lease origin main
# then publish a release tagged <new upstream tag>.1 with the zip asset
```
