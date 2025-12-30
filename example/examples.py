"""
Example usage of the Godbolt API client.

This file demonstrates the various features of the GodboltProject class:
1. Basic preprocessing
2. Multi-file projects with execution
3. Assembly output inspection
4. Macro probe injection (detect preprocessor macro values)
5. Include restoration (preserve #include in preprocessed output)
6. Compiler warnings/errors inspection
7. Local compilation fallback (for compilers without Godbolt execution)
8. Local assembly (compile on Godbolt, assemble locally)
9. Loading source from files
10. Using libraries
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import godbolt
sys.path.insert(0, str(Path(__file__).parent.parent))

from godbolt import GodboltProject


def example_preprocess():
    """Example 1: Simple preprocessor workflow"""
    print("=" * 60)
    print("Example 1: Simple Preprocessor Workflow")
    print("=" * 60)
    
    project = GodboltProject(compiler="cg152", language="c")

    project.set_source(
        """#include <stdio.h>
#define FOO 42

int main() {
    int x = FOO;
    printf("%d\\n", x);
    return 0;
}"""
    )

    result = project.preprocess()
    print(
        "Preprocessed code:",
        result.map(lambda p: p.preprocessed or "<none>").unwrap_or("<failed>"),
    )
    print()


def example_multifile_execute():
    """Example 2: Multi-file project with execution"""
    print("=" * 60)
    print("Example 2: Multi-file Project with Execution")
    print("=" * 60)
    
    project = GodboltProject()

    project.set_source(
        """#include "math_utils.h"
#include <stdio.h>

int main() {
    printf("5 + 3 = %d\\n", add(5, 3));
    printf("5 * 3 = %d\\n", multiply(5, 3));
    return 0;
}"""
    )

    # Add a custom header file
    project.add_file(
        "math_utils.h",
        """#ifndef MATH_UTILS_H
#define MATH_UTILS_H

static inline int add(int a, int b) { return a + b; }
static inline int multiply(int a, int b) { return a * b; }

#endif
""",
    )

    # First get preprocessed output
    result = project.preprocess()
    result.match(
        lambda _: print("Multi-file preprocessed successfully!"),
        lambda e: print(f"Preprocess failed: {e}"),
    )

    # Now execute the same project
    result = project.execute()
    result.match(
        lambda _: print(
            f"Exit code: {project.exit_code}\n"
            f"Output:\n{project.stdout}\n"
            f"Execution time: {project.exec_time} ms"
        ),
        lambda e: print(f"Execute failed: {e}"),
    )
    print()


def example_assembly():
    """Example 3: Assembly inspection with different syntax options"""
    print("=" * 60)
    print("Example 3: Assembly Output Inspection")
    print("=" * 60)
    
    project = GodboltProject(compiler_args="-O2")

    project.set_source(
        """int add(int a, int b) {
    return a + b;
}"""
    )

    # Get assembly with AT&T syntax (default, GNU as compatible)
    result = project.compile(intel_syntax=False)
    result.match(
        lambda _: print("AT&T Syntax Assembly:\n" + (project.assembly or "<none>")),
        lambda e: print(f"Compile failed: {e}"),
    )
    
    # Get assembly with Intel syntax
    result = project.compile(intel_syntax=True)
    result.match(
        lambda _: print("\nIntel Syntax Assembly:\n" + (project.assembly or "<none>")),
        lambda e: print(f"Compile failed: {e}"),
    )
    print()


def example_macro_probe():
    """Example 4: Macro probe injection to detect preprocessor values"""
    print("=" * 60)
    print("Example 4: Macro Probe Injection")
    print("=" * 60)
    
    project = GodboltProject(compiler="cg152")
    
    project.set_source(
        """#include <stdio.h>

/* Auto-detect C standard version */
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
    #define IMPL_TYPE 1  /* C11+ */
#else
    #define IMPL_TYPE 2  /* Pre-C11 */
#endif

int main(void) {
    printf("Implementation type: %d\\n", IMPL_TYPE);
    return 0;
}"""
    )
    
    # Inject a probe to capture the value of IMPL_TYPE after preprocessing
    project.inject_macro_probe("IMPL_TYPE")
    
    result = project.preprocess()
    if result.is_ok():
        # Get the extracted macro value
        probe_result = project.get_macro_probe_value("IMPL_TYPE")
        probe_result.match(
            lambda val: print(f"Detected IMPL_TYPE = {val}"),
            lambda e: print(f"Could not extract macro: {e}"),
        )
        
        # The probe line is automatically stripped from preprocessed output
        print(f"\nPreprocessed output (probe stripped):\n{project.preprocessed}")
    else:
        print(f"Preprocessing failed: {result.error}")
    print()


def example_restore_includes():
    """Example 5: Preserve #include directives in preprocessed output"""
    print("=" * 60)
    print("Example 5: Restore #include Directives")
    print("=" * 60)
    
    project = GodboltProject(compiler="cg152")
    
    project.set_source(
        """#include <stdio.h>
#include <stdlib.h>

int main(void) {
    printf("Hello!\\n");
    return 0;
}"""
    )
    
    # Preprocess WITHOUT restore_includes (default behavior)
    result = project.preprocess(restore_includes=False)
    if result.is_ok():
        print("Without restore_includes:")
        # #include directives are completely removed
        lines = (project.preprocessed or "").split('\n')[:5]
        print('\n'.join(lines) + "\n...")
    
    # Preprocess WITH restore_includes
    result = project.preprocess(restore_includes=True)
    if result.is_ok():
        print("\nWith restore_includes:")
        # #include directives are preserved in the output
        lines = (project.preprocessed or "").split('\n')[:8]
        print('\n'.join(lines) + "\n...")
    print()


def example_compiler_diagnostics():
    """Example 6: Inspect compiler warnings and errors"""
    print("=" * 60)
    print("Example 6: Compiler Diagnostics")
    print("=" * 60)
    
    # Code with warnings
    project = GodboltProject(compiler="cg152", compiler_args="-Wall")
    
    project.set_source(
        """#include <stdio.h>

int main(void) {
    int unused_var = 42;  /* Unused variable warning */
    printf("Hello\\n");
    return 0;
}"""
    )
    
    result = project.execute()
    if result.is_ok():
        print(f"Exit code: {project.exit_code}")
        print(f"Has warnings: {project.has_warnings()}")
        print(f"Warning count: {project.get_warning_count()}")
        print(f"Compiler stderr:\n{project.compiler_stderr}")
    
    print("\n--- Code with errors ---")
    
    # Code with errors
    project.set_source(
        """int main(void) {
    undeclared_function();
    return 0;
}"""
    )
    
    result = project.execute()
    print(f"Has errors: {project.has_errors()}")
    print(f"Error count: {project.get_error_count()}")
    print(f"Compiler stderr:\n{project.compiler_stderr}")
    print()


def example_local_compile():
    """Example 7: Local compilation fallback
    
    For compilers that support preprocessing on Godbolt but not execution,
    you can preprocess remotely and compile/run locally.
    
    Key: Use restore_includes=True so the local compiler sees #include directives!
    """
    print("=" * 60)
    print("Example 7: Local Compilation Fallback")
    print("=" * 60)
    
    # Use a compiler that might not support execution (SDCC in this example)
    # We preprocess on Godbolt, then compile locally
    project = GodboltProject(compiler="cg152")  # Using GCC for demo
    
    project.set_source(
        """#include <stdio.h>

int main(void) {
    printf("Compiled and run locally!\\n");
    return 42;  /* Non-zero to show returncode works */
}"""
    )
    
    # First preprocess on Godbolt
    result = project.preprocess(filter_headers=True, restore_includes=True)
    if result.is_err():
        print(f"Preprocessing failed: {result.error}")
        return
    
    print("Preprocessed successfully on Godbolt")
    
    # Now compile and run locally
    result = project.preprocess_and_run_locally(
        compiler="gcc",  # Use local GCC
        extra_compile_args=["-Wall"],
    )
    
    result.match(
        lambda r: print(
            f"Local execution:\n"
            f"  stdout: {r['stdout']}"
            f"  stderr: {r['stderr']}"
            f"  returncode: {r['returncode']}"
        ),
        lambda e: print(f"Local execution failed: {e}"),
    )
    print()


def example_local_assembly():
    """Example 8: Local assembly execution
    
    For compilers that generate assembly on Godbolt but don't support execution,
    you can compile to assembly remotely, then assemble and run locally.
    This only works when the target architecture matches your local machine.
    """
    print("=" * 60)
    print("Example 8: Local Assembly Execution")
    print("=" * 60)
    
    project = GodboltProject(compiler="cg152", compiler_args="-O2")
    
    project.set_source(
        """#include <stdio.h>

int main(void) {
    printf("Assembled and run locally!\\n");
    return 0;
}"""
    )
    
    # Compile to assembly on Godbolt (preserve directives for local assembly)
    result = project.compile(
        intel_syntax=False,       # AT&T syntax for GNU as
        filter_directives=False,  # Keep .globl, .section, etc.
        filter_labels=False,      # Keep all labels
        filter_comments=False,    # Keep comments
    )
    
    if result.is_err():
        print(f"Compilation failed: {result.error}")
        return
    
    print(f"Generated assembly ({len(project.assembly or '')} bytes)")
    
    # Assemble and run locally
    result = project.compile_and_run_asm_locally(
        assembler="as",
        linker="gcc",
        extra_link_args=["-no-pie"],  # May be needed on some systems
    )
    
    result.match(
        lambda r: print(
            f"Local execution:\n"
            f"  stdout: {r['stdout']}"
            f"  returncode: {r['returncode']}"
        ),
        lambda e: print(f"Local assembly/execution failed: {e}"),
    )
    print()


def example_load_from_file():
    """Example 9: Load source from a file"""
    print("=" * 60)
    print("Example 9: Load Source from File")
    print("=" * 60)
    
    project = GodboltProject(compiler="cg152")
    
    # Load source from a file
    result = project.load_source(str(Path(__file__).parent / "test_simple.c"))
    
    result.match(
        lambda _: print(f"Loaded source:\n{project.source[:200]}..."),
        lambda e: print(f"Failed to load: {e}"),
    )
    
    # Execute the loaded code
    if result.is_ok():
        exec_result = project.execute()
        exec_result.match(
            lambda _: print(f"\nOutput:\n{project.stdout}"),
            lambda e: print(f"Execution failed: {e}"),
        )
    print()


def example_with_stdin():
    """Example 10: Execute with stdin input and program arguments"""
    print("=" * 60)
    print("Example 10: Execution with stdin and Arguments")
    print("=" * 60)
    
    project = GodboltProject(compiler="cg152")
    
    project.set_source(
        """#include <stdio.h>

int main(int argc, char *argv[]) {
    printf("Arguments received: %d\\n", argc);
    for (int i = 0; i < argc; i++) {
        printf("  argv[%d] = %s\\n", i, argv[i]);
    }
    
    char buffer[100];
    printf("Reading from stdin...\\n");
    if (fgets(buffer, sizeof(buffer), stdin)) {
        printf("Got: %s", buffer);
    }
    return 0;
}"""
    )
    
    result = project.execute(
        program_args=["arg1", "arg2", "hello world"],
        stdin="This is stdin input!\n",
    )
    
    result.match(
        lambda _: print(f"Output:\n{project.stdout}"),
        lambda e: print(f"Execution failed: {e}"),
    )
    print()


def example_different_compilers():
    """Example 11: Using different compilers"""
    print("=" * 60)
    print("Example 11: Different Compilers")
    print("=" * 60)
    
    source = """#include <stdio.h>
int main(void) {
    printf("Hello from compiler!\\n");
    return 0;
}"""
    
    compilers = [
        ("cg152", "GCC 15.2"),
        ("cclang2110", "Clang 21.1"),
        ("cg141", "GCC 14.1"),
    ]
    
    for compiler_id, name in compilers:
        project = GodboltProject(compiler=compiler_id)
        project.set_source(source)
        
        result = project.execute()
        status = "✓" if result.is_ok() and project.exit_code == 0 else "✗"
        print(f"{status} {name}: ", end="")
        result.match(
            lambda _: print(f"exit={project.exit_code}"),
            lambda e: print(f"failed: {e}"),
        )
    print()


def example_chaining():
    """Example 12: Method chaining for fluent API"""
    print("=" * 60)
    print("Example 12: Method Chaining (Fluent API)")
    print("=" * 60)
    
    # Many methods return self for chaining
    project = (
        GodboltProject(compiler="cg152", language="c")
        .set_source("""
#include "config.h"
#include <stdio.h>

int main(void) {
    printf("Value: %d\\n", CONFIG_VALUE);
    return 0;
}
""")
        .add_file("config.h", "#define CONFIG_VALUE 123\n")
        .inject_macro_probe("CONFIG_VALUE")
    )
    
    result = project.preprocess()
    if result.is_ok():
        probe = project.get_macro_probe_value("CONFIG_VALUE")
        probe.match(
            lambda v: print(f"CONFIG_VALUE = {v}"),
            lambda e: print(f"Probe failed: {e}"),
        )
    
    # Execute
    result = project.execute()
    result.match(
        lambda _: print(f"Output: {project.stdout}"),
        lambda e: print(f"Failed: {e}"),
    )
    print()


def run_all_examples():
    """Run all examples"""
    examples = [
        example_preprocess,
        example_multifile_execute,
        example_assembly,
        example_macro_probe,
        example_restore_includes,
        example_compiler_diagnostics,
        example_local_compile,
        example_local_assembly,
        example_load_from_file,
        example_with_stdin,
        example_different_compilers,
        example_chaining,
    ]
    
    for example in examples:
        try:
            example()
        except Exception as e:
            print(f"Example {example.__name__} failed with exception: {e}")
        print()


if __name__ == "__main__":
    # Run specific examples or all
    import sys
    
    if len(sys.argv) > 1:
        # Run specific example by number
        example_map = {
            "1": example_preprocess,
            "2": example_multifile_execute,
            "3": example_assembly,
            "4": example_macro_probe,
            "5": example_restore_includes,
            "6": example_compiler_diagnostics,
            "7": example_local_compile,
            "8": example_local_assembly,
            "9": example_load_from_file,
            "10": example_with_stdin,
            "11": example_different_compilers,
            "12": example_chaining,
        }
        for arg in sys.argv[1:]:
            if arg in example_map:
                example_map[arg]()
            else:
                print(f"Unknown example: {arg}")
                print(f"Available: {', '.join(example_map.keys())}")
    else:
        run_all_examples()
