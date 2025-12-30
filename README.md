# AI SLOP WARNING

This repo is vibed coded AI trash. I don't guarauntee a single thing except that it's currently working for me and my purposes.

# Godbolt Tester

Harness for Godbolt Compiler Explorer. Provides a `GodboltProject` client, a CLI test runner driven by YAML, and examples. Remote compile/execute via Godbolt with optional local assembly/compile fallbacks.

Godbolt is very generous with his rate limit on his *free* api. Please consider a donation to Godbolt if you're going to use this project to run many tests, or consider setting up your own Compiler Expolorer instance. It is free software. His donation link to paypal is on godbolt.org under the 'other' button. If I linked it directly, you should not trust that link.

## Dependencies
- Python 3.10+
- `requests`, `PyYAML`, `tqdm` (see `requirements.txt`)
- Network access to https://godbolt.org

## Quick start
```bash
pip install -r requirements.txt
python runner.py example/test_config.yaml --T
```
Outputs go to `results/` (directory is replaced each run).

## CLI
`python runner.py <config_file> [options]`

Options:
- `-o, --results-dir DIR` (default `results`)
- `-d, --debug` (save raw API responses)
- `-c, --compiler NICK` (filter by nickname; repeatable)
- `-t, --test NAME` (filter by test name/variant; repeatable)
- `-a, --all` (run all variants; otherwise auto variants per group)
- `-T, --table` (emit Markdown table; implies `--all`)
- `--table-file PATH` (table destination; default `results/table.md`)
- `--delay SECONDS` (API pause; default 0.5)
- `--language LANG` (default `c`)

## Configuration
YAML file describing compilers and tests. Example: `example/test_config.yaml`.

Compiler fields:
- `api_name` (Godbolt compiler ID)
- `display_name`
- `nickname`
- `extra_flags`
- `local_asm` plus `assembler`, `assembler_args`, `linker`, `local_linker_args`
- `local_compile` plus `local_compiler`, `local_compiler_args`

Test fields (flat or grouped; group defaults apply):
- `group`
- `file_name`
- `detect_macro`
- `detect_value`
- `prepend_lines`
- `auto`
- `include_in_table`
- `additional_files`
- `include_dirs`

Snippet (grouped):
```yaml
compilers:
  - api_name: cg152
    display_name: GCC 15.2
    nickname: gcc

  - api_name: cg346
    display_name: GCC 3.4.6
    nickname: gcc3
    local_asm: true

  - api_name: sdcc
    display_name: SDCC 4.5.0
    nickname: sdcc
    local_compile: true

tests:
  - group: feature
    detect_macro: FEATURE_IMPL
    file_name: test_simple.c
    additional_files:
      - feature_config.h
    variants:
      - variant: auto
        auto: true
        include_in_table: false
      - variant: modern
        display_name: Modern
        detect_value: 1
        prepend_lines:
          - "#define FORCE_MODERN 1"
      - variant: fallback
        display_name: Fallback
        detect_value: 2
        prepend_lines:
          - "#define FORCE_FALLBACK 1"
```

## Outputs
`results/` contains:
- `summary.json` (all cases)
- `table.md` (if `-T`, `--table`)
- Per-compiler/test subdirs with `preprocessed.c`, `output.s` when present, `run_stdout.txt`, `run_stderr.txt`, `result.json`
- Raw API dumps if `--debug`

## API client examples
`example/examples.py` covers preprocessing, execution, assembly, macro probes, include restoration, local fallbacks, stdin/argv. Run:
```bash
python example/examples.py
```

## Layout
- `runner.py` — CLI runner
- `godbolt.py` — Godbolt client
- `result.py` — Result/Ok/Err helpers
- `example/` — sample config and sources
- `results/` — sample output (regenerated each run)

## Notes
- `--delay` controls API pacing; increase if throttled.
- Local fallbacks assume local toolchain matches target arch.
- `results/` is deleted on each run.
