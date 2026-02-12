"""
Test runner for Godbolt Compiler Explorer tests.

Reads YAML configuration, runs tests across multiple compilers,
logs results, and produces a markdown summary table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import yaml
from tqdm import tqdm

from godbolt import GodboltProject
from result import Result, Ok, Err


# =============================================================================
# Utility Functions
# =============================================================================

def get_compiler_version(compiler_cmd: str) -> Optional[Tuple[str, str]]:
    """
    Get the compiler name and version by running `<compiler> --version`.
    
    Args:
        compiler_cmd: The compiler command (e.g., "gcc", "clang", "cc", "tcc")
    
    Returns:
        A tuple of (name, version) or None if unable to determine.
        Examples: ("gcc", "15.2.1"), ("clang", "21.1.6"), ("tcc", "0.9.28rc")
    """
    try:
        result = subprocess.run(
            [compiler_cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout + result.stderr
        
        # Try to identify the compiler type and extract version
        
        # GCC pattern: "gcc (GCC) 15.2.1" or "gcc version 15.2.1"
        gcc_match = re.search(r'gcc.*?(\d+\.\d+(?:\.\d+)?)', output, re.IGNORECASE)
        if gcc_match:
            return ("gcc", gcc_match.group(1))
        
        # Clang pattern: "clang version 21.1.6"
        clang_match = re.search(r'clang version (\d+\.\d+(?:\.\d+)?)', output, re.IGNORECASE)
        if clang_match:
            return ("clang", clang_match.group(1))
        
        # TCC pattern: "tcc version 0.9.28rc"
        tcc_match = re.search(r'tcc version ([\d.]+\w*)', output, re.IGNORECASE)
        if tcc_match:
            return ("tcc", tcc_match.group(1))
        
        return None
        
    except (subprocess.SubprocessError, OSError):
        return None


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CompilerConfig:
    """
    Configuration for a single compiler.
    
    Local execution modes (mutually exclusive):
      - local_asm: Assemble remote assembly locally (same architecture)
      - local_compile: Compile preprocessed source locally (cross-arch fallback)
      - Neither: Execute on Godbolt (default)
    """
    api_name: str
    display_name: str
    nickname: Optional[str] = None
    extra_flags: List[str] = field(default_factory=list)
    # Local assembly mode: get assembly from Godbolt, assemble & run locally
    local_asm: bool = False
    assembler: str = "as"  # Assembler command for local_asm mode
    assembler_args: List[str] = field(default_factory=list)  # Extra assembler args
    linker: str = "gcc"  # Linker command for local_asm mode
    local_linker_args: List[str] = field(default_factory=list)  # Extra linker args for local_asm
    # Local compile mode: get preprocessed source from Godbolt, compile & run locally
    local_compile: bool = False
    local_compiler: str = "gcc"  # Compiler for local_compile mode
    local_compiler_args: List[str] = field(default_factory=list)  # Extra local compiler args

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CompilerConfig":
        return cls(
            api_name=d["api_name"],
            display_name=d.get("display_name", d["api_name"]),
            nickname=d.get("nickname"),
            extra_flags=d.get("extra_flags", []),
            local_asm=d.get("local_asm", False),
            assembler=d.get("assembler", "as"),
            assembler_args=d.get("assembler_args", []),
            linker=d.get("linker", "gcc"),
            local_linker_args=d.get("local_linker_args", []),
            local_compile=d.get("local_compile", False),
            local_compiler=d.get("local_compiler", "gcc"),
            local_compiler_args=d.get("local_compiler_args", []),
        )


@dataclass
class TestVariant:
    """A single test variant within a group."""
    test_name: str
    variant: str
    group: str
    file_name: str
    display_name: str
    prepend_lines: List[str] = field(default_factory=list)
    detect_macro: Optional[str] = None
    detect_value: Optional[int] = None
    is_auto: bool = False
    include_in_table: bool = True
    # Additional files as (godbolt_name, absolute_path) tuples
    # godbolt_name is the original relative path from YAML, used as filename in Godbolt API
    additional_files: List[Tuple[str, str]] = field(default_factory=list)
    include_dirs: List[str] = field(default_factory=list)  # Directories to include all files from

    @classmethod
    def from_dict(
        cls,
        d: Dict[str, Any],
        group_defaults: Dict[str, Any],
    ) -> "TestVariant":
        """
        Create a TestVariant from a dict, with group defaults applied first.
        
        Group-level fields are used as defaults, variant-level fields override them.
        List fields (prepend_lines, additional_files, include_dirs) are merged,
        with variant items appended to group items.
        """
        # Merge group defaults with variant overrides
        # For simple fields, variant overrides group
        group = group_defaults.get("group", "default")
        variant = d.get("variant") or d.get("name") or d.get("test_name", "")
        test_name = d.get("test_name") or f"{group}_{variant}"
        is_auto = bool(d.get("auto", group_defaults.get("auto", False)))
        
        # file_name: variant overrides group
        file_name = d.get("file_name", group_defaults.get("file_name", ""))
        
        # display_name: variant overrides group, fallback to variant name
        display_name = d.get("display_name", group_defaults.get("display_name", variant))
        
        # detect_macro: variant overrides group
        detect_macro = d.get("detect_macro", group_defaults.get("detect_macro"))
        
        # detect_value: variant overrides group
        detect_value = d.get("detect_value", group_defaults.get("detect_value"))
        
        # include_in_table: variant overrides group, default based on is_auto
        if "include_in_table" in d:
            include_in_table = d["include_in_table"]
        elif "include_in_table" in group_defaults:
            include_in_table = group_defaults["include_in_table"]
        else:
            include_in_table = not is_auto
        
        # List fields: merge group + variant (variant appended, deduped)
        def merge_lists(group_key: str, variant_key: str = None) -> List[str]:
            if variant_key is None:
                variant_key = group_key
            result = list(group_defaults.get(group_key, []))
            for item in d.get(variant_key, []):
                if item not in result:
                    result.append(item)
            return result

        def merge_lists_multi_keys(keys: List[str]) -> List[str]:
            """Merge list values from multiple equivalent keys (group + variant)."""
            result: List[str] = []

            for key in keys:
                for item in group_defaults.get(key, []):
                    if item not in result:
                        result.append(item)

            for key in keys:
                for item in d.get(key, []):
                    if item not in result:
                        result.append(item)

            return result
        
        prepend_lines = merge_lists("prepend_lines")
        # additional_files: store as (godbolt_name, path) tuples
        # Initially both are the original relative path; resolve_file_paths updates the absolute path
        additional_files_raw = merge_lists("additional_files")
        additional_files = [(f, f) for f in additional_files_raw]
        # Support both include_dirs and include_directories for compatibility.
        include_dirs = merge_lists_multi_keys(["include_dirs", "include_directories"])
        
        return cls(
            test_name=test_name,
            variant=variant,
            group=group,
            file_name=file_name,
            display_name=display_name,
            prepend_lines=prepend_lines,
            detect_macro=detect_macro,
            detect_value=detect_value,
            is_auto=is_auto,
            include_in_table=include_in_table,
            additional_files=additional_files,
            include_dirs=include_dirs,
        )


@dataclass
class TestResult:
    """Result of running a single test with a compiler."""
    test_name: str
    group: str
    variant: str
    variant_display: str
    is_auto: bool
    detect_value: Optional[int]
    compiler_nickname: Optional[str]
    compiler_display: str
    compiler_api: str
    stage: str  # "preprocessing", "compilation", "runtime", "success"
    passed: bool
    has_warnings: bool
    has_errors: bool
    api_error: bool
    impl_value: Optional[int]  # Detected macro value
    files: Dict[str, str]  # Paths to output files
    stderr: Dict[str, str]  # stderr at each stage

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_name": self.test_name,
            "group": self.group,
            "variant": self.variant,
            "variant_display": self.variant_display,
            "is_auto": self.is_auto,
            "detect_value": self.detect_value,
            "compiler": {
                "nickname": self.compiler_nickname,
                "display_name": self.compiler_display,
                "api_name": self.compiler_api,
            },
            "stage": self.stage,
            "passed": self.passed,
            "warnings": self.has_warnings,
            "errors": self.has_errors,
            "api_error": self.api_error,
            "impl_value": self.impl_value,
            "files": self.files,
            "stderr": self.stderr,
        }


# =============================================================================
# Config Loading
# =============================================================================

def load_config(config_file: str) -> Dict[str, Any]:
    """Load YAML configuration file."""
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_compilers(config: Dict[str, Any]) -> List[CompilerConfig]:
    """Parse compiler configurations from config."""
    return [CompilerConfig.from_dict(c) for c in config.get("compilers", [])]


def parse_tests(config: Dict[str, Any]) -> List[TestVariant]:
    """
    Parse test configurations, supporting both grouped and flat formats.
    
    All fields can appear at either group or variant level. Group-level fields
    serve as defaults for all variants; variant-level fields override them.
    List fields (prepend_lines, additional_files, include_dirs) are merged.
    
    Grouped format:
        tests:
          - group: impl
            detect_macro: IMPL_TYPE
            file_name: test.c           # Shared by all variants
            additional_files:           # Group-level files shared by all variants
              - myheader.h
            include_dirs:               # Directories to include all files from
              - ./headers
            variants:
              - variant: auto
                auto: true
              - variant: native
                detect_value: 1
                additional_files:       # Variant-specific files (merged with group)
                  - extra.h
    
    Flat format:
        tests:
          - test_name: my_test
            file_name: test.c
            additional_files:
              - header.h
    """
    tests: List[TestVariant] = []
    config_tests = config.get("tests", [])
    
    for entry in config_tests:
        if "variants" in entry:
            # Grouped format - extract group defaults (everything except 'variants')
            group_defaults = {k: v for k, v in entry.items() if k != "variants"}
            group_defaults.setdefault("group", "default")
            
            for variant_dict in entry["variants"]:
                tests.append(TestVariant.from_dict(variant_dict, group_defaults))
        else:
            # Flat format - entry is both the group and the variant
            group_defaults = {"group": entry.get("group", "default")}
            tests.append(TestVariant.from_dict(entry, group_defaults))
    
    return tests


def resolve_file_paths(tests: List[TestVariant], base_dir: str) -> None:
    """Resolve relative file paths in tests to absolute paths.

    Paths are resolved against the current working directory (where the
    runner is invoked).
    
    For additional_files, preserves the godbolt_name (original relative path)
    while resolving the filesystem path to an absolute path.
    """
    for test in tests:
        if not os.path.isabs(test.file_name):
            test.file_name = os.path.abspath(os.path.join(base_dir, test.file_name))
        # Resolve additional files: (godbolt_name, path) -> (godbolt_name, absolute_path)
        test.additional_files = [
            (godbolt_name, path if os.path.isabs(path) else os.path.abspath(os.path.join(base_dir, path)))
            for godbolt_name, path in test.additional_files
        ]
        # Resolve include directories
        test.include_dirs = [
            d if os.path.isabs(d) else os.path.abspath(os.path.join(base_dir, d))
            for d in test.include_dirs
        ]


def load_test_files(test: TestVariant) -> List[Tuple[str, str]]:
    """
    Load additional files for a test (e.g., headers).
    
    Loads files from:
      - additional_files: Explicit file paths (with preserved godbolt names)
      - include_dirs: All files from specified directories
    
    Args:
        test: The test variant with file paths already resolved
    
    Returns:
        List of (filename, contents) tuples ready to add to GodboltProject.
        For additional_files, filename is the original relative path from YAML
        (to preserve directory structure like "VA_OPT_polyfill/va_opt.h").
    """
    result = []
    seen_filenames = set()
    
    def _resolve_additional_file_path(godbolt_name: str, filepath: str, include_dirs: List[str]) -> Optional[str]:
        """Resolve an additional file path, searching include_dirs when needed."""
        # 1) Explicit path as configured (already absolute if resolve_file_paths ran)
        if os.path.isfile(filepath):
            return filepath

        # 2) Search include directories for either full relative path or basename
        base_name = os.path.basename(godbolt_name)
        for include_dir in include_dirs:
            candidate_full = os.path.join(include_dir, godbolt_name)
            if os.path.isfile(candidate_full):
                return candidate_full

            candidate_base = os.path.join(include_dir, base_name)
            if os.path.isfile(candidate_base):
                return candidate_base

        return None

    # Load explicitly listed additional files
    # additional_files is List[Tuple[godbolt_name, absolute_path]]
    for godbolt_name, filepath in test.additional_files:
        if godbolt_name in seen_filenames:
            continue
        resolved_path = _resolve_additional_file_path(godbolt_name, filepath, test.include_dirs)
        if not resolved_path:
            print(
                f"Warning: Could not read file {filepath} (also not found in include directories)",
                file=sys.stderr,
            )
            continue
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                contents = f.read()
            result.append((godbolt_name, contents))
            seen_filenames.add(godbolt_name)
        except OSError as e:
            print(f"Warning: Could not read file {resolved_path}: {e}", file=sys.stderr)
    
    # Load all files from include directories
    for include_dir in test.include_dirs:
        if not os.path.isdir(include_dir):
            print(f"Warning: Include directory does not exist: {include_dir}", file=sys.stderr)
            continue
        try:
            for entry in os.listdir(include_dir):
                filepath = os.path.join(include_dir, entry)
                if not os.path.isfile(filepath):
                    continue
                filename = entry
                if filename in seen_filenames:
                    continue
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        contents = f.read()
                    result.append((filename, contents))
                    seen_filenames.add(filename)
                except OSError as e:
                    print(f"Warning: Could not read file {filepath}: {e}", file=sys.stderr)
        except OSError as e:
            print(f"Warning: Could not list directory {include_dir}: {e}", file=sys.stderr)
    
    return result


# =============================================================================
# Test Execution
# =============================================================================

def run_test(
    test: TestVariant,
    compiler: CompilerConfig,
    results_dir: str,
    language: str = "c",
    delay: float = 0.5,
    debug: bool = False,
) -> TestResult:
    """
    Run a single test with a single compiler.
    
    Args:
        test: The test variant to run (includes any additional files)
        compiler: The compiler configuration
        results_dir: Directory to store results
        language: Programming language (default: "c")
        delay: Delay between API requests in seconds
        debug: Save full API responses for debugging
    
    Returns a TestResult with all outcomes and file paths.
    """
    # Create output directory for this test/compiler combo
    safe_compiler_name = compiler.display_name.replace(" ", "_").replace("/", "_")
    subdir = os.path.join(results_dir, f"{test.test_name}_{safe_compiler_name}")
    os.makedirs(subdir, exist_ok=True)
    
    # Output file paths
    files = {
        "preprocessed": os.path.join(subdir, "preprocessed.c"),
        "preprocess_err": os.path.join(subdir, "preprocess_err.txt"),
        "compile_err": os.path.join(subdir, "compile_err.txt"),
        "run_stdout": os.path.join(subdir, "run_stdout.txt"),
        "run_stderr": os.path.join(subdir, "run_stderr.txt"),
        "result": os.path.join(subdir, "result.json"),
    }
    if debug:
        files["debug_response"] = os.path.join(subdir, "debug_response.json")
    
    stderr_log: Dict[str, str] = {"preprocess": "", "compile": "", "run": ""}
    warnings_detected = False
    
    # Read source file
    try:
        with open(test.file_name, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError as e:
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            api_error=True, error_msg=f"Failed to read source: {e}"
        )
    
    # Prepend lines if configured
    if test.prepend_lines:
        source = "\n".join(test.prepend_lines) + "\n" + source
    
    # Build compiler args
    extra_flags = list(compiler.extra_flags) if compiler.extra_flags else []
    
    # For Clang with local_asm, add -fno-integrated-as to emit GNU as-compatible assembly
    if compiler.local_asm and 'clang' in compiler.api_name.lower():
        if '-fno-integrated-as' not in extra_flags:
            extra_flags.append('-fno-integrated-as')
    
    compiler_args = " ".join(extra_flags) if extra_flags else ""
    
    # Create project and inject macro probe if needed
    project = GodboltProject(
        source=source,
        compiler=compiler.api_name,
        language=language,
        compiler_args=compiler_args,
    )
    
    # Add additional files (e.g., headers) from the test config
    if test.additional_files or test.include_dirs:
        for filename, contents in load_test_files(test):
            project.add_file(filename, contents)
    
    if test.detect_macro:
        project.inject_macro_probe(test.detect_macro)
    
    # Run preprocessing
    result = project.preprocess(
        filter_headers=True,
        restore_includes=True,
        trim_empty_lines=True,
    )
    
    # Rate limiting delay
    time.sleep(delay)
    
    if result.is_err():
        stderr_log["preprocess"] = result.error
        _write_file(files["preprocess_err"], result.error)
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            api_error=True, error_msg=result.error
        )
    
    # Save debug response if requested
    if debug and project.response:
        _write_json(files["debug_response"], project.response)
    
    # Check for preprocessing errors
    stderr_log["preprocess"] = project.compiler_stderr
    warnings_detected = project.has_warnings()
    if project.has_errors():
        _write_file(files["preprocess_err"], project.compiler_stderr)
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            has_warnings=warnings_detected, has_errors=True
        )
    
    # Get preprocessed output
    preprocessed = project.preprocessed
    if not preprocessed or not preprocessed.strip():
        _write_file(files["preprocess_err"], "No preprocessed output")
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            has_warnings=warnings_detected
        )
    
    # Save preprocessed source
    _write_file(files["preprocessed"], preprocessed)
    
    # Extract macro probe value if applicable
    impl_value = None
    if test.detect_macro:
        probe_result = project.get_macro_probe_value(test.detect_macro)
        if probe_result.is_ok():
            impl_value = probe_result.value
    
    # Execute the code based on compiler configuration
    if compiler.local_asm:
        # Mode 1: Local assembly - compile on Godbolt, assemble & run locally (same arch)
        # Use unfiltered assembly to preserve .globl and other necessary directives
        compile_result = project.compile(
            intel_syntax=False,
            filter_directives=False,
            filter_labels=False,
            filter_comments=False,
        )
        time.sleep(delay)
        
        if compile_result.is_err():
            stderr_log["compile"] = compile_result.error
            _write_file(files["compile_err"], compile_result.error)
            return _make_result(
                test, compiler, "compilation", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected or project.has_warnings(), api_error=True
            )
        
        # Check for compilation errors
        if project.has_errors():
            stderr_log["compile"] = project.compiler_stderr
            _write_file(files["compile_err"], project.compiler_stderr)
            return _make_result(
                test, compiler, "compilation", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected or project.has_warnings(), has_errors=True
            )
        
        # Save assembly output
        assembly = project.assembly or ""
        files["assembly"] = os.path.join(os.path.dirname(files["preprocessed"]), "output.s")
        _write_file(files["assembly"], assembly)
        warnings_detected = warnings_detected or project.has_warnings()
        
        # Assemble and run locally
        exec_result = project.compile_and_run_asm_locally(
            assembler=compiler.assembler,
            linker=compiler.linker,
            extra_asm_args=compiler.assembler_args or None,
            extra_link_args=compiler.local_linker_args or None,
        )
        
        if exec_result.is_err():
            stderr_log["compile"] = exec_result.error
            _write_file(files["compile_err"], exec_result.error)
            return _make_result(
                test, compiler, "compilation", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected or project.has_warnings()
            )
        
        local_result = exec_result.value
        stdout = local_result.get("stdout", "")
        stderr = local_result.get("stderr", "")
        returncode = local_result.get("returncode", -1)
        
        _write_file(files["run_stdout"], stdout)
        _write_file(files["run_stderr"], stderr)
        stderr_log["run"] = stderr
        warnings_detected = warnings_detected or project.has_warnings() or bool(stderr)
        
        if returncode != 0:
            return _make_result(
                test, compiler, "runtime", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected
            )
        
        return _make_result(
            test, compiler, "success", True, files, stderr_log,
            impl_value=impl_value, has_warnings=warnings_detected
        )
    
    elif compiler.local_compile:
        # Mode 2: Local compile - preprocess on Godbolt, compile & run locally (cross-arch)
        exec_result = project.preprocess_and_run_locally(
            compiler=compiler.local_compiler,
            extra_compile_args=compiler.local_compiler_args or None,
        )
        
        if exec_result.is_err():
            stderr_log["compile"] = exec_result.error
            _write_file(files["compile_err"], exec_result.error)
            return _make_result(
                test, compiler, "compilation", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected or project.has_warnings()
            )
        
        local_result = exec_result.value
        stdout = local_result.get("stdout", "")
        stderr = local_result.get("stderr", "")
        returncode = local_result.get("returncode", -1)
        
        _write_file(files["run_stdout"], stdout)
        _write_file(files["run_stderr"], stderr)
        stderr_log["run"] = stderr
        warnings_detected = warnings_detected or project.has_warnings() or bool(stderr)
        
        if returncode != 0:
            return _make_result(
                test, compiler, "runtime", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected
            )
        
        return _make_result(
            test, compiler, "success", True, files, stderr_log,
            impl_value=impl_value, has_warnings=warnings_detected
        )
    
    else:
        # Mode 3 (default): Godbolt execution
        exec_result = project.execute()
        time.sleep(delay)
        
        if exec_result.is_err():
            stderr_log["compile"] = exec_result.error
            _write_file(files["compile_err"], exec_result.error)
            return _make_result(
                test, compiler, "compilation", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected or project.has_warnings(), api_error=True
            )
        
        # Check if execution actually happened
        if not project.response or not project.response.get("didExecute"):
            stderr_log["compile"] = project.compiler_stderr
            if project.has_errors():
                _write_file(files["compile_err"], project.compiler_stderr)
                return _make_result(
                    test, compiler, "compilation", False, files, stderr_log,
                    impl_value=impl_value, has_warnings=warnings_detected or project.has_warnings(), has_errors=True
                )
        
        stdout = project.stdout or ""
        stderr = project.stderr or ""
        exit_code = project.exit_code
        
        _write_file(files["run_stdout"], stdout)
        _write_file(files["run_stderr"], stderr)
        stderr_log["run"] = stderr
        warnings_detected = warnings_detected or project.has_warnings() or bool(stderr)
        
        if exit_code != 0:
            return _make_result(
                test, compiler, "runtime", False, files, stderr_log,
                impl_value=impl_value, has_warnings=warnings_detected
            )
        
        return _make_result(
            test, compiler, "success", True, files, stderr_log,
            impl_value=impl_value, has_warnings=warnings_detected
        )


def run_preprocess_only(
    test: TestVariant,
    compiler: CompilerConfig,
    results_dir: str,
    language: str = "c",
    delay: float = 0.5,
    debug: bool = False,
) -> TestResult:
    """
    Run only preprocessing for a single test with a single compiler.
    
    Args:
        test: The test variant to run (includes any additional files)
        compiler: The compiler configuration
        results_dir: Directory to store results
        language: Programming language (default: "c")
        delay: Delay between API requests in seconds
        debug: Save full API responses for debugging
    
    Returns a TestResult with preprocessing outcomes and file paths.
    """
    # Create output directory for this test/compiler combo
    safe_compiler_name = compiler.display_name.replace(" ", "_").replace("/", "_")
    subdir = os.path.join(results_dir, f"{test.test_name}_{safe_compiler_name}")
    os.makedirs(subdir, exist_ok=True)
    
    # Output file paths
    files = {
        "preprocessed": os.path.join(subdir, "preprocessed.c"),
        "preprocess_err": os.path.join(subdir, "preprocess_err.txt"),
        "result": os.path.join(subdir, "result.json"),
    }
    if debug:
        files["debug_response"] = os.path.join(subdir, "debug_response.json")
    
    stderr_log: Dict[str, str] = {"preprocess": "", "compile": "", "run": ""}
    warnings_detected = False
    
    # Read source file
    try:
        with open(test.file_name, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError as e:
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            api_error=True, error_msg=f"Failed to read source: {e}"
        )
    
    # Prepend lines if configured
    if test.prepend_lines:
        source = "\n".join(test.prepend_lines) + "\n" + source
    
    # Build compiler args
    extra_flags = list(compiler.extra_flags) if compiler.extra_flags else []
    compiler_args = " ".join(extra_flags) if extra_flags else ""
    
    # Create project and inject macro probe if needed
    project = GodboltProject(
        source=source,
        compiler=compiler.api_name,
        language=language,
        compiler_args=compiler_args,
    )
    
    # Add additional files (e.g., headers) from the test config
    if test.additional_files or test.include_dirs:
        for filename, contents in load_test_files(test):
            project.add_file(filename, contents)
    
    if test.detect_macro:
        project.inject_macro_probe(test.detect_macro)
    
    # Run preprocessing
    result = project.preprocess(
        filter_headers=True,
        restore_includes=True,
        trim_empty_lines=True,
    )
    
    # Rate limiting delay
    time.sleep(delay)
    
    if result.is_err():
        stderr_log["preprocess"] = result.error
        _write_file(files["preprocess_err"], result.error)
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            api_error=True, error_msg=result.error
        )
    
    # Save debug response if requested
    if debug and project.response:
        _write_json(files["debug_response"], project.response)
    
    # Check for preprocessing errors
    stderr_log["preprocess"] = project.compiler_stderr
    warnings_detected = project.has_warnings()
    has_errors = project.has_errors()
    
    if has_errors:
        _write_file(files["preprocess_err"], project.compiler_stderr)
    
    # Get preprocessed output
    preprocessed = project.preprocessed
    if not preprocessed or not preprocessed.strip():
        _write_file(files["preprocess_err"], "No preprocessed output")
        return _make_error_result(
            test, compiler, "preprocessing", files, stderr_log,
            has_warnings=warnings_detected
        )
    
    # Save preprocessed source
    _write_file(files["preprocessed"], preprocessed)
    
    # Extract macro probe value if applicable
    impl_value = None
    if test.detect_macro:
        probe_result = project.get_macro_probe_value(test.detect_macro)
        if probe_result.is_ok():
            impl_value = probe_result.value
    
    # Preprocessing-only mode: success means no errors during preprocessing
    return _make_result(
        test, compiler, "preprocessing", not has_errors, files, stderr_log,
        impl_value=impl_value, has_warnings=warnings_detected, has_errors=has_errors
    )


def _make_result(
    test: TestVariant,
    compiler: CompilerConfig,
    stage: str,
    passed: bool,
    files: Dict[str, str],
    stderr: Dict[str, str],
    impl_value: Optional[int] = None,
    has_warnings: bool = False,
    has_errors: bool = False,
    api_error: bool = False,
) -> TestResult:
    """Create a TestResult and save it to JSON."""
    result = TestResult(
        test_name=test.test_name,
        group=test.group,
        variant=test.variant,
        variant_display=test.display_name,
        is_auto=test.is_auto,
        detect_value=test.detect_value,
        compiler_nickname=compiler.nickname,
        compiler_display=compiler.display_name,
        compiler_api=compiler.api_name,
        stage=stage,
        passed=passed,
        has_warnings=has_warnings,
        has_errors=has_errors,
        api_error=api_error,
        impl_value=impl_value,
        files=files,
        stderr=stderr,
    )
    _write_json(files["result"], result.to_dict())
    return result


def _make_error_result(
    test: TestVariant,
    compiler: CompilerConfig,
    stage: str,
    files: Dict[str, str],
    stderr: Dict[str, str],
    api_error: bool = False,
    has_warnings: bool = False,
    has_errors: bool = False,
    error_msg: str = "",
) -> TestResult:
    """Create an error TestResult."""
    if error_msg and not stderr.get("preprocess"):
        stderr["preprocess"] = error_msg
    return _make_result(
        test, compiler, stage, False, files, stderr,
        has_warnings=has_warnings, has_errors=has_errors, api_error=api_error
    )


def _write_file(path: str, content: str) -> None:
    """Write content to a file."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _write_json(path: str, data: Any) -> None:
    """Write JSON data to a file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# =============================================================================
# Markdown Table Generation
# =============================================================================

def status_icon(result: Optional[TestResult]) -> str:
    """
    Generate emoji status for a test result.
    
    Legend:
      ✅ passed
      ❌ failed to compile/preprocess
      ⚠️ runtime failure
      ℹ️ warnings present (appended)
      — no result / API error
    """
    if result is None:
        return "—"
    
    if result.api_error:
        return ""  # Skip API errors in table
    
    if result.passed:
        icon = "✅"
    elif result.stage in ("preprocessing", "compilation"):
        icon = "❌"
    else:
        icon = "⚠️"
    
    # Add warning indicator for passed tests with warnings
    if result.has_warnings and result.passed:
        icon += "ℹ️"
    
    return icon


def build_markdown_table(
    results: List[TestResult],
    compilers: List[CompilerConfig],
    tests: List[TestVariant],
    output_path: str,
) -> None:
    """
    Generate a markdown table summarizing test results.
    
    - Rows are compilers
    - Columns are test variants (excluding auto tests)
    - A ⭐ marks variants whose detect_value matches the auto test's impl_value
    - Footnotes are added for compilers using local_compile or local_asm modes
    """
    # Determine columns (non-auto variants that should appear in table)
    columns: List[TestVariant] = [t for t in tests if t.include_in_table and not t.is_auto]
    
    # Check if we have multiple groups
    groups = list(set(t.group for t in tests))
    multi_group = len(groups) > 1
    
    # Build lookup: compiler_display -> group -> variant -> result
    lookup: Dict[str, Dict[str, Dict[str, TestResult]]] = {}
    for r in results:
        lookup.setdefault(r.compiler_display, {}).setdefault(r.group, {})[r.variant] = r
    
    # Track which compilers need footnotes and detect local compiler versions
    # Map: (mode, local_version) -> marker
    footnote_configs: Dict[Tuple[str, str], str] = {}
    # Map: compiler_display -> (marker, mode, local_version)
    footnote_map: Dict[str, Tuple[str, str, str]] = {}
    footnote_markers = ["*", "**", "***", "****"]  # Support up to 4 different modes
    next_marker_idx = 0
    
    for compiler in compilers:
        if compiler.local_compile:
            local_version = get_compiler_version(compiler.local_compiler)
            version_str = f"{local_version[0]} {local_version[1]}" if local_version else compiler.local_compiler
            config_key = ("local_compile", version_str)
            
            # Reuse existing marker or create new one
            if config_key not in footnote_configs:
                if next_marker_idx < len(footnote_markers):
                    footnote_configs[config_key] = footnote_markers[next_marker_idx]
                    next_marker_idx += 1
            
            if config_key in footnote_configs:
                footnote_map[compiler.display_name] = (
                    footnote_configs[config_key],
                    "local_compile",
                    version_str
                )
        elif compiler.local_asm:
            # For local_asm, the linker is typically the local compiler
            local_version = get_compiler_version(compiler.linker)
            version_str = f"{local_version[0]} {local_version[1]}" if local_version else compiler.linker
            config_key = ("local_asm", version_str)
            
            # Reuse existing marker or create new one
            if config_key not in footnote_configs:
                if next_marker_idx < len(footnote_markers):
                    footnote_configs[config_key] = footnote_markers[next_marker_idx]
                    next_marker_idx += 1
            
            if config_key in footnote_configs:
                footnote_map[compiler.display_name] = (
                    footnote_configs[config_key],
                    "local_asm",
                    version_str
                )
    
    # Collect rows
    rows: List[List[str]] = []
    
    # Header row
    header = ["CC"]
    for t in columns:
        label = f"{t.group}:{t.display_name}" if multi_group else t.display_name
        header.append(label)
    rows.append(header)
    
    # Data rows
    for compiler in compilers:
        groups_map = lookup.get(compiler.display_name, {})
        
        # Find auto impl values per group
        auto_vals: Dict[str, int] = {}
        for group, variants in groups_map.items():
            for result in variants.values():
                if result.is_auto and result.impl_value is not None:
                    auto_vals[group] = result.impl_value
        
        # Build compiler name with footnote marker if needed
        compiler_name = compiler.display_name
        if compiler.display_name in footnote_map:
            marker, _, _ = footnote_map[compiler.display_name]
            compiler_name = f"{compiler.display_name}{marker}"
        
        row = [compiler_name]
        for t in columns:
            result = groups_map.get(t.group, {}).get(t.variant)
            cell = status_icon(result)
            
            # Add star if this variant matches the auto-detected impl
            auto_val = auto_vals.get(t.group)
            if auto_val is not None and t.detect_value is not None and auto_val == t.detect_value:
                cell = "⭐" + cell
            
            row.append(cell)
        rows.append(row)
    
    # Calculate column widths (accounting for emoji display width)
    def visual_len(s: str) -> int:
        # These emojis typically render as 2 characters wide
        extras = sum(s.count(c) for c in ["✅", "❌", "⭐", "⚠️", "ℹ️"])
        return len(s) + extras
    
    col_widths = [0] * len(header)
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], visual_len(cell))
    
    # Format rows
    def format_row(cells: List[str]) -> str:
        padded = []
        for i, cell in enumerate(cells):
            pad = col_widths[i] - visual_len(cell)
            padded.append(cell + " " * pad)
        return "| " + " | ".join(padded) + " |"
    
    lines = [format_row(rows[0])]
    lines.append("| " + " | ".join("-" * w for w in col_widths) + " |")
    for row in rows[1:]:
        lines.append(format_row(row))
    
    # Add footnotes if any
    if footnote_map:
        lines.append("")  # Blank line before footnotes
        # Collect unique footnotes (avoid duplicates)
        seen_footnotes: Dict[str, Tuple[str, str]] = {}  # marker -> (mode, local_version)
        for compiler in compilers:
            if compiler.display_name in footnote_map:
                marker, mode, local_version = footnote_map[compiler.display_name]
                if marker not in seen_footnotes:
                    seen_footnotes[marker] = (mode, local_version)
        
        # Output footnotes in order
        for marker in footnote_markers:
            if marker in seen_footnotes:
                mode, local_version = seen_footnotes[marker]
                # Escape the marker to prevent Markdown bullet interpretation
                escaped_marker = "\\" + marker
                if mode == "local_compile":
                    lines.append(
                        f"{escaped_marker} This compiler was only used for preprocessing and then "
                        f"the result was compiled locally with {local_version}.  "
                    )
                elif mode == "local_asm":
                    lines.append(
                        f"{escaped_marker} This compiler outputted assembly which was then "
                        f"assembled and run locally with {local_version}.  "
                    )
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# =============================================================================
# Main Entry Point
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run tests across multiple compilers via Godbolt and generate reports."
    )
    parser.add_argument("config_file", help="Path to YAML config file")
    parser.add_argument(
        "--results-dir", "-o", default="results",
        help="Directory for output files (default: results)"
    )
    parser.add_argument(
        "--debug", "-d", action="store_true",
        help="Save full API responses for debugging"
    )
    parser.add_argument(
        "--compiler", "-c", action="append", metavar="NICKNAME",
        help="Filter by compiler nickname (can be repeated)"
    )
    parser.add_argument(
        "--test", "-t", action="append", metavar="NAME",
        help="Filter by test name or variant (can be repeated)"
    )
    parser.add_argument(
        "--group", "-g", action="append", metavar="GROUP",
        help="Filter by test group (can be repeated)"
    )
    parser.add_argument(
        "--all", "-a", action="store_true",
        help="Run all tests (default: only auto tests)"
    )
    parser.add_argument(
        "--table", "-T", action="store_true",
        help="Generate markdown summary table (implies --all)"
    )
    parser.add_argument(
        "--table-file", default=None,
        help="Path for markdown table (default: results/table.md)"
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Delay between API requests in seconds (default: 0.5)"
    )
    parser.add_argument(
        "--language", default="c",
        help="Programming language (default: c)"
    )
    parser.add_argument(
        "--preprocess-only", "-P", action="store_true",
        help="Only run preprocessing and save results (no compilation or execution)"
    )
    
    args = parser.parse_args()
    
    # Load config
    try:
        config = load_config(args.config_file)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        return 1
    
    # Parse compilers and tests
    compilers = parse_compilers(config)
    tests = parse_tests(config)
    resolve_file_paths(tests, os.getcwd())
    
    # Apply filters
    if args.compiler:
        compilers = [c for c in compilers if c.nickname in args.compiler]
        if not compilers:
            print(f"Error: No compilers matching: {args.compiler}", file=sys.stderr)
            return 1
    
    if args.test:
        tests = [t for t in tests if t.test_name in args.test or t.variant in args.test]
        if not tests:
            print(f"Error: No tests matching: {args.test}", file=sys.stderr)
            return 1
    
    if args.group:
        tests = [t for t in tests if t.group in args.group]
        if not tests:
            print(f"Error: No tests matching groups: {args.group}", file=sys.stderr)
            return 1
    
    # --table implies --all
    run_all = args.all or args.table
    
    # If not running all, filter to auto tests (or all if no auto exists in group)
    if not run_all and not args.test:
        groups_with_auto = {t.group for t in tests if t.is_auto}
        tests = [t for t in tests if t.is_auto or t.group not in groups_with_auto]
    
    if not compilers or not tests:
        print("Error: No compilers or tests to run.", file=sys.stderr)
        return 1
    
    # Prepare results directory
    if os.path.exists(args.results_dir):
        shutil.rmtree(args.results_dir)
    os.makedirs(args.results_dir, exist_ok=True)
    
    # Run tests
    # Calculate total for progress bar - exclude auto tests if running all (they'll be covered by specific variants)
    auto_tests = [t for t in tests if t.is_auto]
    non_auto_tests = [t for t in tests if not t.is_auto]
    
    if run_all and non_auto_tests:
        # When running all, auto tests are redundant with their matching variants
        effective_tests = len(non_auto_tests) * len(compilers)
    else:
        # When running only auto tests, count them
        effective_tests = len(tests) * len(compilers)
    
    passed = 0
    results: List[TestResult] = []
    
    # Track auto test results to avoid redundant runs
    # Map: (compiler_display, group) -> {impl_value: TestResult}
    auto_results: Dict[Tuple[str, str], Dict[int, TestResult]] = {}
    
    # Create progress bar
    with tqdm(total=effective_tests, desc="Running tests", unit="test") as pbar:
        for test in tests:
            for compiler in compilers:
                # Check if this test can be skipped because an auto test already ran it
                skip_key = (compiler.display_name, test.group)
                if not test.is_auto and test.detect_value is not None:
                    if skip_key in auto_results:
                        auto_variants = auto_results[skip_key]
                        if test.detect_value in auto_variants:
                            # Reuse the auto test result with this test's variant info
                            auto_result = auto_variants[test.detect_value]
                            # Create a copy with the current test's variant details
                            reused_result = replace(
                                auto_result,
                                test_name=test.test_name,
                                variant=test.variant,
                                variant_display=test.display_name,
                                is_auto=False,
                                detect_value=test.detect_value,
                            )
                            results.append(reused_result)
                            if reused_result.passed:
                                passed += 1
                            pbar.update(1)
                            continue
                
                if args.preprocess_only:
                    result = run_preprocess_only(
                        test=test,
                        compiler=compiler,
                        results_dir=args.results_dir,
                        language=args.language,
                        delay=args.delay,
                        debug=args.debug,
                    )
                else:
                    result = run_test(
                        test=test,
                        compiler=compiler,
                        results_dir=args.results_dir,
                        language=args.language,
                        delay=args.delay,
                        debug=args.debug,
                    )
                results.append(result)
                
                # Track auto test results
                if test.is_auto and result.impl_value is not None:
                    if skip_key not in auto_results:
                        auto_results[skip_key] = {}
                    auto_results[skip_key][result.impl_value] = result
                
                # Only count non-auto tests toward passed when running all
                # (auto tests will be counted when their matching variant is skipped)
                if run_all and non_auto_tests and test.is_auto:
                    pass  # Don't count auto tests - they'll be counted via skip logic
                elif result.passed:
                    passed += 1
                
                if not result.passed:
                    # Print failure on a new line (tqdm-compatible)
                    tqdm.write(f"✗ {test.test_name} on {compiler.display_name} (stage: {result.stage})")
                
                # Don't update progress bar for auto tests when running all
                if not (run_all and non_auto_tests and test.is_auto):
                    pbar.update(1)
    
    # Save summary
    summary_path = os.path.join(args.results_dir, "summary.json")
    _write_json(summary_path, [r.to_dict() for r in results])
    
    # Print summary
    print(f"\nResults: {passed}/{effective_tests} passed")
    if passed == effective_tests:
        print("All tests passed!")
    
    # Generate table if requested
    if args.table:
        table_path = args.table_file or os.path.join(args.results_dir, "table.md")
        build_markdown_table(results, compilers, tests, table_path)
        print(f"Table written to: {table_path}")
    
    return 0 if passed == effective_tests else 1


if __name__ == "__main__":
    sys.exit(main())
