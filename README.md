# pxt-compile

`pxt-compile.py` packs a MakeCode Arcade project folder into either:

- a Magic PNG that can be imported into MakeCode
- a source-embedded UF2

## Usage

```powershell
py pxt-compile.py .\project-folder .\packed.png --format png
py pxt-compile.py .\project-folder .\packed.uf2 --format uf2
```

## Notes

- The tool reads `pxt.json` and packs `pxt.json` plus the files listed in `files`.
- `--include-tests` also packs `testFiles`.
- The PNG path can grow vertically as needed, and the UF2 path can emit as many blocks as needed, so the tool is not limited by the usual editor export size caps.
