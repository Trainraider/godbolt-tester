/* Simple test file for runner verification */

#include <stdio.h>
#include "feature_config.h"

/* Feature detection / selection */
#if defined(FORCE_MODERN)
    #define FEATURE_IMPL 1
#elif defined(FORCE_FALLBACK)
    #define FEATURE_IMPL 2
#elif defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
    /* C11 or later - use modern impl */
    #define FEATURE_IMPL 1
#else
    /* Older standard - use fallback */
    #define FEATURE_IMPL 2
#endif

#if FEATURE_IMPL == 1
    #define GREETING "Hello from modern implementation!"
#else
    #define GREETING "Hello from fallback implementation!"
    #define TEST_RESULT 1
#endif

#ifndef TEST_RESULT
    #define TEST_RESULT 0
#endif

int main(void) {
    printf("%s\n", GREETING);
    printf("FEATURE_IMPL = %d\n", FEATURE_IMPL);
    printf("Project: %s v%d.%d\n", PROJECT_NAME, VERSION_MAJOR, VERSION_MINOR);
    printf("5 + 3 = %d\n", add(5, 3));
    return TEST_RESULT;
}
