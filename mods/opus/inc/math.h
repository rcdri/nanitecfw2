#pragma once
/* fixed-point build: only constants / never-called float helpers */
#define M_PI 3.141592653
static inline float fabsf(float x){return x<0?-x:x;}
