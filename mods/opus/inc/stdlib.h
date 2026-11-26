#pragma once
#include <stddef.h>
static inline int abs(int x){return x<0?-x:x;}
void *malloc(size_t);
void *realloc(void *, size_t);
void *calloc(size_t, size_t);
void free(void *);
