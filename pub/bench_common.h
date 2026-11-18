#ifndef BENCH_COMMON_H
#define BENCH_COMMON_H
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>

static uint64_t now_us(clockid_t clock_id) {
    struct timespec ts;
    if (clock_gettime(clock_id, &ts)) { perror("clock_gettime"); exit(2); }
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}
static int positive_number(const char *s, int allow_zero) {
    char *end; errno = 0;
    long n = strtol(s, &end, 10);
    if (errno || !*s || *end || n < (allow_zero ? 0 : 1) || n > 10000000) return -1;
    return (int)n;
}
static int valid_run(const char *run) {
    if (!*run || strlen(run)>20) return 0;
    for (const char *p=run; *p; ++p)
        if (!( (*p>='A'&&*p<='Z') || (*p>='a'&&*p<='z') || (*p>='0'&&*p<='9') || *p=='_' )) return 0;
    return 1;
}
static int make_payload(char *payload, const char *run, int seq, uint64_t ts) {
    int n=snprintf(payload,129,"{\"v\":1,\"run_id\":\"%s\",\"device_id\":\"pi01\",\"seq\":%d,\"ts_us\":%" PRIu64 ",\"temp\":27.5,\"pad\":\"",run,seq,ts);
    if (n<0 || n>126) return -1;
    memset(payload+n,'x',126-n); payload[126]='"'; payload[127]='}'; payload[128]=0;
    return 0;
}
static const char *env_default(const char *name, const char *fallback) {
    const char *v=getenv(name); return v && *v ? v : fallback;
}
static void log_connect(const char *run,const char *mode,int success,uint64_t elapsed,uint64_t start_wall,uint64_t end_wall) {
    printf("{\"event\":\"connect\",\"run_id\":\"%s\",\"transport\":\"%s\",\"success\":%s,\"connect_to_connack_us\":%" PRIu64 ",\"start_realtime_us\":%" PRIu64 ",\"connack_realtime_us\":%" PRIu64 "}\n",run,mode,success?"true":"false",elapsed,start_wall,end_wall);
    fflush(stdout);
}
#endif
