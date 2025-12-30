"""
Godbolt Compiler Explorer API client.

Provides a GodboltProject class for preprocessing, compiling, and executing code
via the Godbolt API, with Result-based error handling (no exceptions raised to caller).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import requests
from typing import Optional, Dict, Any, List, Tuple

from result import Ok, Err, Result


class GodboltProject:
    """
    A class to manage source files and interact with Compiler Explorer (Godbolt) API.

    This class stores source files and caches the last API response, allowing you
    to conveniently access different aspects of the compilation/execution results.

    All API methods return Result[GodboltProject, Err] instead of raising exceptions.
    """

    _BASE_URL = "https://godbolt.org/api/compiler"

    def __init__(
        self,
        source: str = "",
        compiler: str = "cg152",
        language: str = "c",
        compiler_args: str = "",
    ):
        """
        Initialize a Godbolt project.

        Args:
            source: Main source code
            compiler: Compiler ID (default: cg152 for GCC 15.2.0)
            language: Programming language (default: "c")
            compiler_args: Compiler arguments/flags
        """
        self.source = source
        self.compiler = compiler
        self.language = language
        self.compiler_args = compiler_args
        self.files: List[Dict[str, str]] = []
        self.libraries: List[Dict[str, str]] = []
        self._last_response: Optional[Dict[str, Any]] = None
        self._original_source: Optional[str] = None  # Store source before probe insertion
        self._include_probes: List[Tuple[str, str]] = []  # Store (start_marker, original_include)
        self._macro_probes: List[str] = []  # Store macro names being probed
        self._macro_probe_values: Dict[str, int] = {}  # Cache extracted probe values

    def set_source(self, source: str) -> GodboltProject:
        """Set the main source code."""
        self.source = source
        return self

    def load_source(self, filepath: str) -> Result[GodboltProject]:
        """Load main source code from a file."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                self.source = f.read()
            return Ok(self)
        except OSError as e:
            return Err(f"Failed to read {filepath}: {e}")

    def add_file(self, filename: str, contents: str) -> GodboltProject:
        """
        Add an additional source file (e.g., header file).

        Args:
            filename: Name of the file (e.g., "myheader.h")
            contents: Contents of the file
        """
        self.files.append({"filename": filename, "contents": contents})
        return self

    def add_file_from_path(
        self, filepath: str, filename: Optional[str] = None
    ) -> Result[GodboltProject]:
        """
        Add a file from the filesystem.

        Args:
            filepath: Path to the file to read
            filename: Name to use in the project (defaults to basename of filepath)
        """
        import os

        if filename is None:
            filename = os.path.basename(filepath)

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                contents = f.read()
        except OSError as e:
            return Err(f"Failed to read {filepath}: {e}")

        return Ok(self.add_file(filename, contents))

    def add_library(self, library_id: str, version: str) -> GodboltProject:
        """
        Add a library to link against.

        Args:
            library_id: Library identifier (e.g., "openssl")
            version: Library version (e.g., "111c")
        """
        self.libraries.append({"id": library_id, "version": version})
        return self

    def clear_files(self) -> GodboltProject:
        """Clear all additional files."""
        self.files = []
        return self

    def clear_libraries(self) -> GodboltProject:
        """Clear all libraries."""
        self.libraries = []
        return self

    def _base_payload(self) -> Dict[str, Any]:
        """Return the common payload skeleton shared by all API calls."""
        return {
            "source": self.source,
            "compiler": self.compiler,
            "lang": self.language,
            "files": self.files,
            "bypassCache": False,
            "allowStoreCodeDebug": True,
        }

    def _base_options(
        self,
        *,
        execute_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return the common 'options' sub-dict, with sensible defaults."""
        return {
            "userArguments": self.compiler_args,
            "tools": [],
            "libraries": self.libraries,
            "executeParameters": execute_params or {"args": [], "stdin": ""},
        }

    @staticmethod
    def _encode_header_name(header: str) -> str:
        """
        Encode a header file path for use in function names.
        
        Args:
            header: The header file path (e.g., "stdio.h" or "mylib/utils.h")
            
        Returns:
            Encoded string with special characters replaced
        """
        encoded = header.replace(".", "__PERIOD")
        encoded = encoded.replace("/", "__SLASH")
        encoded = encoded.replace("\\", "__BACKSLASH")
        return encoded

    def _insert_include_probes(self) -> str:
        """
        Insert probe markers around #include directives in the source code.
        
        Returns:
            Modified source code with probe markers inserted
        """
        self._original_source = self.source
        self._include_probes = []
        
        # Regex to match #include directives
        # Captures: (optional whitespace)(#include)(<|")( header path )(>|")
        include_pattern = re.compile(
            r'^(\s*)(#\s*include\s*)([<"])([^>"]+)([>"])',
            re.MULTILINE
        )
        
        lines = self.source.split('\n')
        modified_lines = []
        probe_counter = 1
        
        for line in lines:
            match = include_pattern.match(line)
            if match:
                indent = match.group(1)
                include_directive = match.group(2)
                open_bracket = match.group(3)
                header_path = match.group(4)
                close_bracket = match.group(5)
                
                # Determine if it's system (<>) or local ("")
                include_type = "system" if open_bracket == '<' else "local"
                
                # Encode the header name
                encoded_header = self._encode_header_name(header_path)
                
                # Create marker function names
                start_marker = f"__godbolt_start_probe{probe_counter}_{include_type}_{encoded_header}"
                end_marker = f"__godbolt_end_probe{probe_counter}_{include_type}_{encoded_header}"
                
                # Store the mapping
                original_include = f"{include_directive}{open_bracket}{header_path}{close_bracket}"
                self._include_probes.append((start_marker, original_include))
                
                # Insert the probed code (use (void) for C standard compliance)
                modified_lines.append(f"{indent}void {start_marker}(void);")
                modified_lines.append(line)
                modified_lines.append(f"{indent}void {end_marker}(void);")
                
                probe_counter += 1
            else:
                modified_lines.append(line)
        
        return '\n'.join(modified_lines)

    def _restore_includes_from_preprocessed(self, preprocessed: str) -> str:
        """
        Restore original #include directives in preprocessed output by replacing
        probe markers and their contents.
        
        Args:
            preprocessed: The preprocessed source code from the API
            
        Returns:
            Modified preprocessed code with original #include directives restored
        """
        result = preprocessed
        
        for start_marker, original_include in self._include_probes:
            # Extract probe number and header info from start marker
            # Pattern: __godbolt_start_probe{N}_{type}_{encoded_header}
            match = re.match(r'__godbolt_start_probe(\d+)_(.+)', start_marker)
            if not match:
                continue
            
            probe_num = match.group(1)
            end_marker = start_marker.replace('_start_', '_end_')
            
            # Escape backslashes in the replacement string to avoid re.sub() interpreting them
            safe_replacement = original_include.replace('\\', '\\\\')
            
            # Create regex patterns for different scenarios:
            # Match (void) or () for compatibility
            # 1. Both markers present (normal case)
            pattern_both = re.compile(
                rf'void\s+{re.escape(start_marker)}\s*\(\s*(?:void\s*)?\)\s*;.*?void\s+{re.escape(end_marker)}\s*\(\s*(?:void\s*)?\)\s*;',
                re.DOTALL
            )
            
            # 2. Only start marker present (e.g., missing header file)
            pattern_start_only = re.compile(
                rf'void\s+{re.escape(start_marker)}\s*\(\s*(?:void\s*)?\)\s*;',
                re.DOTALL
            )
            
            # Try to replace with both markers first
            new_result = pattern_both.sub(safe_replacement, result)
            
            # If nothing was replaced, try start marker only
            if new_result == result:
                new_result = pattern_start_only.sub(safe_replacement, result)
            
            result = new_result
        
        return result

    def inject_macro_probe(self, macro_name: str) -> GodboltProject:
        """
        Inject a probe to capture the value of a macro after preprocessing.
        
        The probe creates a line like:
            int __GODBOLT_MACRO_PROBE_MACRONAME__ = (int)(MACRONAME);
        
        After preprocessing, use `get_macro_probe_value(macro_name)` to extract
        the integer value the macro expanded to.
        
        Args:
            macro_name: The name of the macro to probe
            
        Returns:
            self for chaining
        """
        if macro_name not in self._macro_probes:
            self._macro_probes.append(macro_name)
            probe_line = f"\nint __GODBOLT_MACRO_PROBE_{macro_name}__ = (int)({macro_name});\n"
            self.source = self.source.rstrip('\n') + probe_line
        return self

    def get_macro_probe_value(self, macro_name: str) -> Result[int]:
        """
        Get the value of a probed macro.
        
        Values are extracted and cached during preprocess(), so this just
        returns the cached value.
        
        Args:
            macro_name: The macro name that was probed with inject_macro_probe()
            
        Returns:
            Ok(int) with the macro's integer value, or Err if not found/parseable
        """
        if macro_name in self._macro_probe_values:
            return Ok(self._macro_probe_values[macro_name])
        
        return Err(f"No cached value for macro '{macro_name}'; was it probed and preprocessed?")

    def _extract_macro_probe_value(self, text: str, macro_name: str) -> Optional[int]:
        """
        Extract a macro probe value from text.
        
        Args:
            text: The preprocessed source code
            macro_name: The macro name to extract
            
        Returns:
            The integer value, or None if not found/parseable
        """
        # Match: __GODBOLT_MACRO_PROBE_NAME__ = (int)(VALUE) or just = VALUE
        # Supports decimal, hex, and optional casts
        pattern = rf"__GODBOLT_MACRO_PROBE_{re.escape(macro_name)}__\s*=\s*(?:\([^)]*\)\s*)?(?:\(?\s*(-?0x[0-9a-fA-F]+|-?\d+)\s*\)?)"
        match = re.search(pattern, text)
        
        if not match:
            return None
        
        literal = match.group(1)
        try:
            return int(literal, 0)  # base 0 auto-detects hex/dec
        except ValueError:
            return None

    def _extract_and_cache_macro_probes(self, text: str) -> None:
        """
        Extract all macro probe values from text and cache them.
        
        Args:
            text: The preprocessed source code (before stripping probes)
        """
        self._macro_probe_values = {}
        for macro_name in self._macro_probes:
            value = self._extract_macro_probe_value(text, macro_name)
            if value is not None:
                self._macro_probe_values[macro_name] = value

    def clear_macro_probes(self) -> GodboltProject:
        """Clear all macro probes from tracking (does not modify source)."""
        self._macro_probes = []
        self._macro_probe_values = {}
        return self

    def _strip_macro_probes_from_output(self, text: str) -> str:
        """
        Remove macro probe lines from preprocessed output.
        
        Args:
            text: The preprocessed source code
            
        Returns:
            Text with macro probe lines removed
        """
        if not self._macro_probes:
            return text
        
        lines = text.split('\n')
        filtered_lines = []
        
        for line in lines:
            # Check if this line is a macro probe
            is_probe = False
            for macro_name in self._macro_probes:
                if f"__GODBOLT_MACRO_PROBE_{macro_name}__" in line:
                    is_probe = True
                    break
            
            if not is_probe:
                filtered_lines.append(line)
        
        return '\n'.join(filtered_lines)

    def _post(self, payload: Dict[str, Any]) -> Result[Dict[str, Any]]:
        """POST to the compile endpoint and return the JSON response or an Err."""
        url = f"{self._BASE_URL}/{self.compiler}/compile"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
        except requests.RequestException as e:
            return Err(f"Network error: {e}")

        if not resp.ok:
            return Err(
                f"HTTP {resp.status_code}: {resp.reason}",
                status_code=resp.status_code,
            )

        try:
            return Ok(resp.json())
        except ValueError as e:
            return Err(f"Invalid JSON in response: {e}")
    
    def preprocess(
        self,
        filter_headers: bool = True,
        clang_format: bool = False,
        trim_empty_lines: bool = True,
        restore_includes: bool = False,
    ) -> Result[GodboltProject]:
        """
        Run the preprocessor and store the result.

        Args:
            filter_headers: Whether to filter out header content
            clang_format: Whether to apply clang-format to output
            trim_empty_lines: Whether to trim leading/trailing empty lines
            restore_includes: Whether to insert probes and restore original #include directives

        Returns:
            Ok(self) on success, Err on failure.
        """
        # If restore_includes is enabled, insert probe markers
        if restore_includes:
            modified_source = self._insert_include_probes()
            original_source = self.source
            self.source = modified_source
        
        payload = self._base_payload()
        payload["options"] = {
            **self._base_options(),
            "compilerOptions": {
                "producePp": {
                    "filter-headers": filter_headers,
                    "clang-format": clang_format,
                },
                "produceGccDump": {},
                "produceOptInfo": False,
                "produceCfg": False,
                "produceIr": None,
                "produceClangir": None,
                "produceOptPipeline": None,
                "produceDevice": False,
                "produceYul": None,
                "overrides": [],
            },
            "filters": {
                "binaryObject": False,
                "binary": False,
                "execute": False,
                "intel": True,
                "demangle": True,
                "labels": True,
                "libraryCode": True,
                "directives": True,
                "commentOnly": True,
                "trim": False,
                "debugCalls": False,
            },
        }

        result = self._post(payload)
        
        # Restore original source if we modified it
        if restore_includes:
            self.source = original_source
        
        if result.is_err():
            return result

        self._last_response = result.value

        # Restore original includes in preprocessed output if enabled
        if restore_includes:
            pp = self._last_response.get("ppOutput", {})
            if "output" in pp:
                pp["output"] = self._restore_includes_from_preprocessed(pp["output"])

        # Extract macro probe values BEFORE stripping them from output
        if self._macro_probes:
            pp = self._last_response.get("ppOutput", {})
            if "output" in pp:
                self._extract_and_cache_macro_probes(pp["output"])
                pp["output"] = self._strip_macro_probes_from_output(pp["output"])

        # Trim preprocessed output if requested
        if trim_empty_lines:
            pp = self._last_response.get("ppOutput", {})
            if "output" in pp:
                pp["output"] = pp["output"].strip()

        return Ok(self)
    
    def compile(
        self,
        intel_syntax: bool = False,
        filter_directives: bool = True,
        filter_labels: bool = True,
        filter_comments: bool = True,
    ) -> Result[GodboltProject]:
        """
        Compile the code to assembly and store the result.

        Args:
            intel_syntax: Use Intel assembly syntax (default: False for AT&T/GNU as compatible)
            filter_directives: Remove assembler directives (default: True)
                              Set to False for local assembly to preserve .globl etc.
            filter_labels: Remove unused labels (default: True)
            filter_comments: Remove comment-only lines (default: True)

        Returns:
            Ok(self) on success, Err on failure.
        """
        payload = self._base_payload()
        payload["options"] = {
            **self._base_options(),
            "compilerOptions": {
                "skipAsm": False,
                "executorRequest": False,
                "overrides": [],
            },
            "filters": {
                "binary": False,
                "binaryObject": False,
                "commentOnly": filter_comments,
                "demangle": True,
                "directives": filter_directives,
                "execute": False,
                "intel": intel_syntax,
                "labels": filter_labels,
                "libraryCode": False,
                "trim": False,
                "debugCalls": False,
            },
        }

        result = self._post(payload)
        if result.is_err():
            return result

        self._last_response = result.value
        return Ok(self)
    
    def execute(
        self,
        program_args: Optional[List[str]] = None,
        stdin: str = "",
    ) -> Result[GodboltProject]:
        """
        Compile and execute the code, storing the result.

        Args:
            program_args: Arguments to pass to the program
            stdin: Standard input to provide to the program

        Returns:
            Ok(self) on success, Err on failure.
        """
        exec_params = {
            "args": program_args or [],
            "stdin": stdin,
            "runtimeTools": [],
        }
        payload = self._base_payload()
        payload["options"] = {
            **self._base_options(execute_params=exec_params),
            "compilerOptions": {"executorRequest": True},
            "filters": {"execute": True},
        }

        result = self._post(payload)
        if result.is_err():
            return result

        self._last_response = result.value
        return Ok(self)

    # -------------------------------------------------------------------------
    # Property accessors for the last response
    # -------------------------------------------------------------------------
    @property
    def response(self) -> Optional[Dict[str, Any]]:
        """Get the raw last API response."""
        return self._last_response

    # Synchronous getters that return Result[...] where Err indicates caller misuse
    # (no response available) and Ok carries the requested value. Properties below
    # are thin convenience wrappers that return simple Optionals or bools.

    def get_preprocessed(self) -> Result[str]:
        if not self._last_response:
            return Err("No response available; call preprocess() first")
        pp = self._last_response.get("ppOutput")
        if not pp or "output" not in pp:
            return Err("No preprocessed output in last response")
        return Ok(pp.get("output"))

    @property
    def preprocessed(self) -> Optional[str]:
        return self.get_preprocessed().unwrap_or(None)

    def get_assembly(self) -> Result[str]:
        if not self._last_response:
            return Err("No response available; call compile() first")
        asm = self._last_response.get("asm")
        if not asm:
            return Ok("")
        return Ok("\n".join(line.get("text", "") for line in asm))

    @property
    def assembly(self) -> Optional[str]:
        return self.get_assembly().unwrap_or(None)

    def get_assembly_lines(self) -> Result[List[Dict[str, Any]]]:
        if not self._last_response:
            return Err("No response available; call compile() first")
        asm = self._last_response.get("asm")
        if not asm:
            return Ok([])
        return Ok(asm)

    @property
    def assembly_lines(self) -> Optional[List[Dict[str, Any]]]:
        return self.get_assembly_lines().unwrap_or(None)

    def get_stdout(self) -> Result[str]:
        if not self._last_response:
            return Err("No response available; call execute() first")
        stdout_lines = self._last_response.get("stdout")
        if not stdout_lines:
            return Ok("")
        text = "\n".join(line.get("text", "") for line in stdout_lines if "text" in line)
        return Ok(text)

    @property
    def stdout(self) -> Optional[str]:
        return self.get_stdout().unwrap_or(None)

    def get_stderr(self) -> Result[str]:
        if not self._last_response:
            return Err("No response available; call execute() first")
        stderr_lines = self._last_response.get("stderr")
        if not stderr_lines:
            return Ok("")
        text = "\n".join(line.get("text", "") for line in stderr_lines if "text" in line)
        return Ok(text)

    @property
    def stderr(self) -> Optional[str]:
        return self.get_stderr().unwrap_or(None)

    def get_exit_code(self) -> Result[int]:
        if not self._last_response:
            return Err("No response available; call execute() first")
        code = self._last_response.get("code")
        if code is None:
            return Err("No exit code present in last response")
        return Ok(int(code))

    @property
    def exit_code(self) -> Optional[int]:
        return self.get_exit_code().unwrap_or(None)

    def get_exec_time(self) -> Result[int]:
        if not self._last_response:
            return Err("No response available; call execute() or compile() first")
        et = self._last_response.get("execTime")
        if et is None:
            return Err("No execTime present in last response")
        return Ok(int(et))

    @property
    def exec_time(self) -> Optional[int]:
        return self.get_exec_time().unwrap_or(None)

    def get_build_exec_time(self) -> Result[int]:
        if not self._last_response:
            return Err("No response available; call execute() first")
        br = self._last_response.get("buildResult")
        if not br or "execTime" not in br:
            return Err("No buildResult.execTime present in last response")
        return Ok(int(br.get("execTime")))

    @property
    def build_exec_time(self) -> Optional[int]:
        return self.get_build_exec_time().unwrap_or(None)

    def get_compilation_succeeded(self) -> Result[bool]:
        if not self._last_response:
            return Err("No response available; call preprocess() or compile() first")
        return Ok(self._last_response.get("code", -1) == 0)

    @property
    def compilation_succeeded(self) -> bool:
        return self.get_compilation_succeeded().unwrap_or(False)

    def get_execution_succeeded(self) -> Result[bool]:
        if not self._last_response:
            return Err("No response available; call execute() first")
        if not self._last_response.get("didExecute"):
            return Ok(False)
        return Ok(self._last_response.get("code", -1) == 0)

    @property
    def execution_succeeded(self) -> bool:
        return self.get_execution_succeeded().unwrap_or(False)

    # -------------------------------------------------------------------------
    # Compiler messages (warnings/errors)
    # -------------------------------------------------------------------------

    def _get_compiler_streams(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return (stderr, stdout) lists for compiler diagnostics.

        For execute() responses, compiler output lives under buildResult.* while
        top-level stdout/stderr are the program output. For preprocess/compile-only
        calls, diagnostics are at the top level.
        """
        if not self._last_response:
            return ([], [])

        build = self._last_response.get("buildResult")
        if build and ("stderr" in build or "stdout" in build):
            return build.get("stderr", []) or [], build.get("stdout", []) or []

        return self._last_response.get("stderr", []) or [], self._last_response.get("stdout", []) or []
    
    def get_compiler_messages(self) -> Result[List[Dict[str, Any]]]:
        """
        Get structured compiler messages (warnings, errors, notes).
        
        Returns:
            Ok(list of message dicts) with keys like 'text', 'tag' (if available)
        """
        if not self._last_response:
            return Err("No response available")

        stderr_lines, stdout_lines = self._get_compiler_streams()
        return Ok((stderr_lines or []) + (stdout_lines or []))

    @property
    def compiler_messages(self) -> List[Dict[str, Any]]:
        """Get compiler messages as a list of dicts."""
        return self.get_compiler_messages().unwrap_or([])

    def get_compiler_stderr(self) -> Result[str]:
        """
        Get compiler stderr as a single string.
        
        This differs from get_stderr() which is for program execution output.
        This is for compiler output during preprocessing/compilation.
        """
        if not self._last_response:
            return Err("No response available")

        stderr_lines, stdout_lines = self._get_compiler_streams()
        lines = stderr_lines if stderr_lines else stdout_lines
        if not lines:
            return Ok("")

        text = "\n".join(line.get("text", "") for line in lines if "text" in line)
        return Ok(text)

    @property
    def compiler_stderr(self) -> str:
        """Get compiler stderr as a string."""
        return self.get_compiler_stderr().unwrap_or("")

    def has_errors(self) -> bool:
        """
        Check if compilation produced errors (failed to compile).
        """
        if not self._last_response:
            return False
        return self._last_response.get("code", 0) != 0

    def has_warnings(self) -> bool:
        """
        Check if compilation produced warnings.
        
        Heuristic: looks for 'warning' in stderr output.
        """
        stderr_lines, stdout_lines = self._get_compiler_streams()
        text_chunks = []
        for lines in (stderr_lines, stdout_lines):
            if lines:
                text_chunks.append("\n".join(line.get("text", "") for line in lines if "text" in line))

        if not text_chunks:
            return False

        return bool(re.search(r'\bwarning\b', "\n".join(text_chunks), re.IGNORECASE))

    def get_error_count(self) -> int:
        """
        Count the number of errors in compiler output.
        
        Heuristic: counts lines containing 'error:' pattern.
        """
        stderr = self.compiler_stderr
        if not stderr:
            return 0
        return len(re.findall(r'\berror:', stderr, re.IGNORECASE))

    def get_warning_count(self) -> int:
        """
        Count the number of warnings in compiler output.
        
        Heuristic: counts lines containing 'warning:' pattern.
        """
        stderr = self.compiler_stderr
        if not stderr:
            return 0
        return len(re.findall(r'\bwarning:', stderr, re.IGNORECASE))

    # -------------------------------------------------------------------------
    # Local compilation and execution (fallback for compilers without Godbolt exec)
    # -------------------------------------------------------------------------

    def compile_locally(
        self,
        compiler: str = "gcc",
        output_path: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> Result[str]:
        """
        Compile the preprocessed source locally using a system compiler.
        
        This is a fallback for testing with compilers that don't support
        execution on Godbolt. The source should already be preprocessed.
        
        Note: Any additional files added via add_file() will also be written
        to the temp directory so #include directives can find them.
        
        Args:
            compiler: Local compiler command (default: "gcc")
            output_path: Path for the output executable (default: temp file)
            extra_args: Additional compiler arguments
            
        Returns:
            Ok(path to executable) on success, Err on failure
        """
        preprocessed = self.preprocessed
        if preprocessed is None:
            return Err("No preprocessed source available; call preprocess() first")
        
        # Create temp directory to hold source and any additional files
        temp_dir = tempfile.mkdtemp()
        src_path = os.path.join(temp_dir, "source.c")
        
        try:
            # Write main source file
            with open(src_path, 'w', encoding='utf-8') as src_file:
                src_file.write(preprocessed)
            
            # Write any additional files (headers, etc.) to the same directory
            for file_info in self.files:
                filename = file_info.get("filename", "")
                contents = file_info.get("contents", "")
                if filename:
                    file_path = os.path.join(temp_dir, filename)
                    # Create subdirectories if filename contains path separators
                    file_dir = os.path.dirname(file_path)
                    if file_dir and not os.path.exists(file_dir):
                        os.makedirs(file_dir, exist_ok=True)
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(contents)
            
            # Determine output path
            if output_path is None:
                output_path = tempfile.mktemp(suffix='.exe' if os.name == 'nt' else '')
            
            cmd = [compiler]
            if extra_args:
                cmd.extend(extra_args)
            cmd.extend(['-o', output_path, src_path])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=temp_dir,  # Run from temp dir so includes are found
            )
            
            if result.returncode != 0:
                return Err(f"Local compilation failed:\n{result.stderr}")
            
            return Ok(output_path)
            
        except subprocess.TimeoutExpired:
            return Err("Local compilation timed out")
        except FileNotFoundError:
            return Err(f"Compiler '{compiler}' not found")
        except Exception as e:
            return Err(f"Local compilation error: {e}")
        finally:
            # Clean up temp directory and all files in it
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass

    def execute_locally(
        self,
        executable_path: str,
        program_args: Optional[List[str]] = None,
        stdin: str = "",
        timeout: int = 10,
    ) -> Result[Dict[str, Any]]:
        """
        Execute a locally compiled program.
        
        Args:
            executable_path: Path to the executable
            program_args: Arguments to pass to the program
            stdin: Standard input to provide
            timeout: Execution timeout in seconds
            
        Returns:
            Ok(dict) with 'stdout', 'stderr', 'returncode' on success, Err on failure
        """
        try:
            cmd = [executable_path]
            if program_args:
                cmd.extend(program_args)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                input=stdin if stdin else None,
                timeout=timeout
            )
            
            return Ok({
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode,
            })
            
        except subprocess.TimeoutExpired:
            return Err("Program execution timed out")
        except FileNotFoundError:
            return Err(f"Executable '{executable_path}' not found")
        except Exception as e:
            return Err(f"Execution error: {e}")

    def preprocess_and_run_locally(
        self,
        compiler: str = "gcc",
        program_args: Optional[List[str]] = None,
        stdin: str = "",
        extra_compile_args: Optional[List[str]] = None,
        timeout: int = 10,
    ) -> Result[Dict[str, Any]]:
        """
        Convenience method: preprocess on Godbolt, compile and run locally.
        
        Useful for compilers that support preprocessing on Godbolt but not execution.
        
        Args:
            compiler: Local compiler command (default: "gcc")
            program_args: Arguments to pass to the program
            stdin: Standard input to provide
            extra_compile_args: Additional compiler arguments
            timeout: Execution timeout in seconds
            
        Returns:
            Ok(dict) with 'stdout', 'stderr', 'returncode' on success, Err on failure
        """
        # Compile locally
        compile_result = self.compile_locally(
            compiler=compiler,
            extra_args=extra_compile_args
        )
        if compile_result.is_err():
            return compile_result
        
        exe_path = compile_result.value
        
        try:
            # Execute
            return self.execute_locally(
                executable_path=exe_path,
                program_args=program_args,
                stdin=stdin,
                timeout=timeout
            )
        finally:
            # Clean up executable
            try:
                os.unlink(exe_path)
            except OSError:
                pass

    def _needs_no_pie(self, assembly: str) -> bool:
        """
        Detect if assembly uses non-position-independent patterns.
        
        Older compilers (e.g., GCC 3.x) produce assembly with absolute addresses
        that won't link correctly on modern systems with PIE enabled by default.
        
        Returns True if -no-pie should be added to linker flags.
        """
        import re
        # Look for x86/x86_64 absolute address patterns that indicate non-PIE code
        # Pattern: mov instructions using immediate addresses like $.LC0, $symbol, etc.
        # These are typically: movl $label, %reg or movq $label, %reg
        non_pie_patterns = [
            r'\bmovl?\s+\$\.?[A-Za-z_]',  # movl $.LC0, ... or movl $symbol, ...
            r'\bmovq?\s+\$\.?[A-Za-z_]',  # movq variant
            r'\bpush[lq]?\s+\$\.?[A-Za-z_]',  # pushl $symbol
        ]
        for pattern in non_pie_patterns:
            if re.search(pattern, assembly):
                return True
        return False

    def assemble_locally(
        self,
        assembler: str = "as",
        linker: str = "gcc",
        output_path: Optional[str] = None,
        extra_asm_args: Optional[List[str]] = None,
        extra_link_args: Optional[List[str]] = None,
    ) -> Result[str]:
        """
        Assemble the compiled assembly output locally.
        
        This is for same-architecture scenarios where Godbolt generates assembly
        but doesn't support execution. We assemble and link locally.
        
        Args:
            assembler: Assembler command (default: "as")
            linker: Linker command (default: "gcc")
            output_path: Path for the output executable (default: temp file)
            extra_asm_args: Additional assembler arguments
            extra_link_args: Additional linker arguments
            
        Returns:
            Ok(path to executable) on success, Err on failure
        """
        assembly = self.assembly
        if assembly is None:
            return Err("No assembly output available; call compile() first")
        
        # Create temp file for assembly
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.s', delete=False, encoding='utf-8'
        ) as asm_file:
            asm_file.write(assembly)
            asm_path = asm_file.name
        
        # Create temp file for object
        obj_path = tempfile.mktemp(suffix='.o')
        
        # Determine output path
        if output_path is None:
            output_path = tempfile.mktemp(suffix='.exe' if os.name == 'nt' else '')
        
        try:
            # Assemble
            asm_cmd = [assembler]
            if extra_asm_args:
                asm_cmd.extend(extra_asm_args)
            asm_cmd.extend(['-o', obj_path, asm_path])
            
            result = subprocess.run(
                asm_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                return Err(f"Assembly failed:\n{result.stderr}")
            
            # Auto-detect if we need -no-pie for non-PIE assembly
            link_args = list(extra_link_args) if extra_link_args else []
            if self._needs_no_pie(assembly) and '-no-pie' not in link_args:
                link_args.append('-no-pie')
            
            # Link
            link_cmd = [linker]
            if link_args:
                link_cmd.extend(link_args)
            link_cmd.extend(['-o', output_path, obj_path])
            
            result = subprocess.run(
                link_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                return Err(f"Linking failed:\n{result.stderr}")
            
            return Ok(output_path)
            
        except subprocess.TimeoutExpired:
            return Err("Assembly/linking timed out")
        except FileNotFoundError as e:
            return Err(f"Tool not found: {e}")
        except Exception as e:
            return Err(f"Assembly error: {e}")
        finally:
            # Clean up temp files
            for path in [asm_path, obj_path]:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def compile_and_run_asm_locally(
        self,
        assembler: str = "as",
        linker: str = "gcc",
        program_args: Optional[List[str]] = None,
        stdin: str = "",
        extra_asm_args: Optional[List[str]] = None,
        extra_link_args: Optional[List[str]] = None,
        timeout: int = 10,
    ) -> Result[Dict[str, Any]]:
        """
        Convenience method: compile on Godbolt, assemble and run locally.
        
        For same-architecture scenarios where Godbolt compiles to assembly
        but doesn't support execution.
        
        Args:
            assembler: Assembler command (default: "as")
            linker: Linker command (default: "gcc")
            program_args: Arguments to pass to the program
            stdin: Standard input to provide
            extra_asm_args: Additional assembler arguments
            extra_link_args: Additional linker arguments
            timeout: Execution timeout in seconds
            
        Returns:
            Ok(dict) with 'stdout', 'stderr', 'returncode' on success, Err on failure
        """
        # Assemble locally
        asm_result = self.assemble_locally(
            assembler=assembler,
            linker=linker,
            extra_asm_args=extra_asm_args,
            extra_link_args=extra_link_args,
        )
        if asm_result.is_err():
            return asm_result
        
        exe_path = asm_result.value
        
        try:
            # Execute
            return self.execute_locally(
                executable_path=exe_path,
                program_args=program_args,
                stdin=stdin,
                timeout=timeout
            )
        finally:
            # Clean up executable
            try:
                os.unlink(exe_path)
            except OSError:
                pass
