#define _POSIX_C_SOURCE 200809L
/* Working derivative of benchmark/client.c. MQTT 3.1.1 QoS 0; same EMQX as QUIC. */
#include <mosquitto.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include <arpa/inet.h>
#include <signal.h>
#include <stdbool.h>
#include "bench_common.h"
static int ready, failed, disconnected;
static volatile sig_atomic_t stopped;
static uint64_t connack, connack_wall;
static void stop(int sig) { (void)sig; stopped=1; }
static void keylog(const SSL *ssl,const char *line) {
    (void)ssl; const char *path=getenv("SSLKEYLOGFILE");
    if(path && *path) { FILE *f=fopen(path,"a"); if(f) { fprintf(f,"%s\n",line); fclose(f); } }
}
static void connected(struct mosquitto *m,void *u,int rc) {
    (void)m;(void)u;connack=now_us(CLOCK_MONOTONIC);connack_wall=now_us(CLOCK_REALTIME);ready=rc==0;failed=rc!=0;
}
static void closed(struct mosquitto *m,void *u,int rc) { (void)m;(void)u;(void)rc;disconnected=1; }
static int pump(struct mosquitto *m,int ms) {
    int rc=mosquitto_loop(m,ms,1); if(rc) failed=1; return rc;
}
int main(int argc,char **argv) {
    if(argc!=7) { fprintf(stderr,"usage: tcp-pub tcp|tls host run_id count period_ms timeout_s\n");return 2; }
    const char *mode=argv[1],*host=argv[2],*run=argv[3];
    int tls=!strcmp(mode,"tls"),count=positive_number(argv[4],1),period=positive_number(argv[5],0),timeout=positive_number(argv[6],0);
    if((!tls&&strcmp(mode,"tcp"))||!valid_run(run)||count<0||period<1||timeout<1)return 2;
    signal(SIGINT,stop); signal(SIGTERM,stop); setvbuf(stdout,NULL,_IOLBF,0);
    mosquitto_lib_init();struct mosquitto *m=mosquitto_new(run,true,NULL);if(!m)return 2;
    mosquitto_connect_callback_set(m,connected);mosquitto_disconnect_callback_set(m,closed);
    mosquitto_int_option(m,MOSQ_OPT_PROTOCOL_VERSION,MQTT_PROTOCOL_V311);
    SSL_CTX *ssl=NULL;
    if(tls) {
        const char *ca=env_default("MQTT_CA_FILE","../emqx/certs/ca.crt");
        const char *identity=env_default("MQTT_SERVER_NAME",host);
        ssl=SSL_CTX_new(TLS_client_method());
        if(!ssl||!SSL_CTX_set_min_proto_version(ssl,TLS1_3_VERSION)||!SSL_CTX_set_max_proto_version(ssl,TLS1_3_VERSION)
           ||!SSL_CTX_set_ciphersuites(ssl,"TLS_AES_128_GCM_SHA256")||!SSL_CTX_set1_groups_list(ssl,"X25519")
           ||!SSL_CTX_load_verify_locations(ssl,ca,NULL)) { fprintf(stderr,"TLS configuration/CA failed\n");return 2; }
        SSL_CTX_set_verify(ssl,SSL_VERIFY_PEER,NULL);SSL_CTX_set_keylog_callback(ssl,keylog);SSL_CTX_set_session_cache_mode(ssl,SSL_SESS_CACHE_OFF);
        unsigned char addr[16];X509_VERIFY_PARAM *param=SSL_CTX_get0_param(ssl);
        int ip=inet_pton(AF_INET,identity,addr)==1||inet_pton(AF_INET6,identity,addr)==1;
        if(!(ip?X509_VERIFY_PARAM_set1_ip_asc(param,identity):X509_VERIFY_PARAM_set1_host(param,identity,0)))return 2;
        /* libmosquitto otherwise compares the network address a second time.
           Trust AND identity are mandatory above, in OpenSSL's verifier. */
        if(mosquitto_tls_set(m,ca,NULL,NULL,NULL,NULL)||mosquitto_tls_insecure_set(m,true)
           ||mosquitto_int_option(m,MOSQ_OPT_SSL_CTX_WITH_DEFAULTS,0)||mosquitto_void_option(m,MOSQ_OPT_SSL_CTX,ssl))return 2;
    }
    uint64_t start_wall=now_us(CLOCK_REALTIME),start=now_us(CLOCK_MONOTONIC);
    int rc=mosquitto_connect(m,host,tls?8883:1883,30);if(rc)failed=1;
    while(!ready&&!failed&&!stopped&&now_us(CLOCK_MONOTONIC)-start<(uint64_t)timeout*1000000)pump(m,1);
    if(!ready||connack-start>(uint64_t)timeout*1000000||stopped)failed=1;
    log_connect(run,mode,!failed,ready?connack-start:0,start_wall,connack_wall);
    if(ready&&tls) {
        SSL *s=mosquitto_ssl_get(m);
        printf("{\"event\":\"tls\",\"version\":\"%s\",\"cipher\":\"%s\",\"resumed\":%s,\"verified\":true}\n",SSL_get_version(s),SSL_get_cipher_name(s),SSL_session_reused(s)?"true":"false");
        if(SSL_session_reused(s))failed=1;
    }
    int sent=0;
    if(!failed&&count) {
        uint64_t begin=now_us(CLOCK_MONOTONIC)+2000000;
        for(int seq=0;seq<count&&!failed&&!disconnected&&!stopped;seq++) {
            uint64_t due=begin+(uint64_t)seq*period*1000;
            while(now_us(CLOCK_MONOTONIC)<due&&!failed&&!disconnected&&!stopped)pump(m,1);
            if(failed||disconnected||stopped)break;
            char payload[129];uint64_t ts=now_us(CLOCK_REALTIME);
            if(make_payload(payload,run,seq,ts)){failed=1;break;}
            rc=mosquitto_publish(m,NULL,"quic4iot/pi01/data",128,payload,0,false);
            printf("{\"event\":\"send\",\"seq\":%d,\"ts_us\":%" PRIu64 ",\"rc\":%d}\n",seq,ts,rc);
            if(rc||pump(m,0))failed=1;else sent++;
        }
        uint64_t end=now_us(CLOCK_MONOTONIC)+10000000;
        while(!failed&&!disconnected&&!stopped&&now_us(CLOCK_MONOTONIC)<end)pump(m,1);
    }
    int outcome=failed||disconnected||stopped||!ready||(count&&sent!=count);
    printf("{\"event\":\"end\",\"success\":%s,\"sent\":%d,\"disconnected\":%s}\n",outcome?"false":"true",sent,disconnected?"true":"false");
    mosquitto_disconnect(m);mosquitto_destroy(m);if(ssl)SSL_CTX_free(ssl);mosquitto_lib_cleanup();return outcome?1:0;
}
