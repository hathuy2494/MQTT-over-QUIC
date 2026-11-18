#define _POSIX_C_SOURCE 200809L
/* Working derivative: source package 143005, socket/event-loop + MQTT framing.
   New workload, mandatory certificate validation, JSONL, no resumption. */
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <lsquic.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include "bench_common.h"
struct context {
    int fd,running,ready,failed,completed,connect_logged,tls_ok,subscribed;
    int count,period,timeout,seq,sub;
    const char *run,*topic,*identity;
    uint64_t start,start_wall,connack,next_send,last_send,last_tx;
    struct sockaddr_in local,peer;
    lsquic_engine_t *engine;
    lsquic_conn_t *conn;
    lsquic_stream_t *stream;
    SSL_CTX *ssl;
    unsigned char out[16384],in[8192];
    size_t out_len,in_len;
};
static struct context *active;
static volatile sig_atomic_t stopped;
static void stop(int sig){(void)sig;stopped=1;}
static int log_message(void *ctx,const char *buf,size_t len){(void)ctx;fwrite(buf,1,len,stderr);return 0;}
static const struct lsquic_logger_if logger={.log_buf=log_message};
static void keylog(const SSL *ssl,const char *line){
    (void)ssl;const char *p=getenv("SSLKEYLOGFILE");
    if(p&&*p){FILE *f=fopen(p,"a");if(f){fprintf(f,"%s\n",line);fclose(f);}}
}
static void ssl_info(const SSL *ssl,int where,int ret){
    (void)ret;
    if(where&SSL_CB_HANDSHAKE_DONE){
        const char *cipher=SSL_get_cipher_name(ssl);
        active->tls_ok=SSL_version(ssl)==TLS1_3_VERSION&&!strcmp(cipher,"TLS_AES_128_GCM_SHA256")&&!SSL_session_reused(ssl)&&SSL_get_verify_result(ssl)==X509_V_OK;
        printf("{\"event\":\"tls\",\"version\":\"%s\",\"cipher\":\"%s\",\"resumed\":%s,\"verified\":%s}\n",SSL_get_version(ssl),cipher,SSL_session_reused(ssl)?"true":"false",SSL_get_verify_result(ssl)==X509_V_OK?"true":"false");
    }
}
static SSL_CTX *get_ssl(void *peer_ctx,const struct sockaddr *local){
    (void)local;return ((struct context *)peer_ctx)->ssl;
}
static int packets_out(void *ctx,const struct lsquic_out_spec *specs,unsigned count){
    struct context *c=ctx;unsigned i;
    for(i=0;i<count;i++){
        struct msghdr msg={0};msg.msg_name=(void*)specs[i].dest_sa;
        msg.msg_namelen=sizeof(struct sockaddr_in);msg.msg_iov=specs[i].iov;msg.msg_iovlen=specs[i].iovlen;
        if(sendmsg(c->fd,&msg,0)<0)break;
    }
    return (int)i;
}
static size_t put_string(unsigned char *p,const char *s){
    size_t n=strlen(s);p[0]=(unsigned char)(n>>8);p[1]=(unsigned char)n;memcpy(p+2,s,n);return n+2;
}
static int queue_packet(struct context *c,unsigned char type,const void *body,size_t length){
    unsigned char header[5];size_t n=1,left=length;header[0]=type;
    do{unsigned char b=(unsigned char)(left%128);left/=128;if(left)b|=128;header[n++]=b;}while(left);
    if(c->out_len+n+length>sizeof(c->out)){c->failed=1;return -1;}
    memcpy(c->out+c->out_len,header,n);c->out_len+=n;
    if(length){memcpy(c->out+c->out_len,body,length);c->out_len+=length;}
    if(c->stream)lsquic_stream_wantwrite(c->stream,1);
    return 0;
}
static void send_connect(struct context *c){
    unsigned char b[256];size_t n=put_string(b,"MQTT");
    b[n++]=4;b[n++]=2;b[n++]=0;b[n++]=30;n+=put_string(b+n,c->run);queue_packet(c,0x10,b,n);
}
static void json_string(const unsigned char *p,size_t n){
    putchar('"');for(size_t i=0;i<n;i++){unsigned char ch=p[i];if(ch=='"'||ch=='\\'){putchar('\\');putchar(ch);}else if(ch<32||ch>=127)printf("\\u%04x",ch);else putchar(ch);}putchar('"');
}
static void packet(struct context *c,unsigned char type,const unsigned char *b,size_t n){
    if((type>>4)==2){
        if(c->ready||n!=2||b[1]!=0){c->failed=1;return;}
        c->connack=now_us(CLOCK_MONOTONIC);c->ready=c->tls_ok&&c->connack-c->start<=(uint64_t)c->timeout*1000000;
        log_connect(c->run,"quic",c->ready,c->connack-c->start,c->start_wall,now_us(CLOCK_REALTIME));c->connect_logged=1;
        if(!c->ready){c->failed=1;return;}
        c->next_send=c->connack+2000000;
        if(c->sub){unsigned char sub[256]={0,1};size_t len=2+put_string(sub+2,c->topic);sub[len++]=0;queue_packet(c,0x82,sub,len);}
        else if(c->count==0)c->completed=1;
    }else if((type>>4)==9&&c->sub){
        if(n!=3||b[0]!=0||b[1]!=1||b[2]!=0){c->failed=1;return;}
        c->subscribed=1;printf("{\"event\":\"subscribed\",\"qos\":0}\n");
    }else if((type>>4)==3&&c->sub){
        uint64_t received=now_us(CLOCK_REALTIME);if(n<2)return;
        size_t len=((size_t)b[0]<<8)|b[1],offset=2+len;int qos=(type>>1)&3;
        if(offset>n||qos>1){c->failed=1;return;}
        if(qos){if(offset+2>n){c->failed=1;return;}queue_packet(c,0x40,b+offset,2);offset+=2;}
        printf("{\"event\":\"receive\",\"recv_ts_us\":%" PRIu64 ",\"topic\":",received);json_string(b+2,len);
        printf(",\"payload\":");json_string(b+offset,n-offset);printf("}\n");
    }
}
static lsquic_conn_ctx_t *new_conn(void *ctx,lsquic_conn_t *conn){struct context *c=ctx;c->conn=conn;return (lsquic_conn_ctx_t*)c;}
static void handshake(lsquic_conn_t *conn,enum lsquic_hsk_status status){
    struct context *c=(struct context*)lsquic_conn_get_ctx(conn);
    if(status!=LSQ_HSK_OK||!c->tls_ok){c->failed=1;lsquic_conn_close(conn);return;}
    printf("{\"event\":\"quic_handshake\",\"elapsed_us\":%" PRIu64 "}\n",now_us(CLOCK_MONOTONIC)-c->start);
    lsquic_conn_make_stream(conn);
}
static void conn_closed(lsquic_conn_t *conn){struct context *c=(struct context*)lsquic_conn_get_ctx(conn);c->conn=NULL;c->stream=NULL;c->running=0;if(!c->completed)c->failed=1;}
static lsquic_stream_ctx_t *new_stream(void *ctx,lsquic_stream_t *stream){
    struct context *c=ctx;if(c->stream){c->failed=1;return NULL;}c->stream=stream;
    lsquic_stream_wantread(stream,1);send_connect(c);return (lsquic_stream_ctx_t*)c;
}
static void write_stream(lsquic_stream_t *stream,lsquic_stream_ctx_t *h){
    struct context *c=(struct context*)h;if(!c)return;
    if(c->out_len){ssize_t n=lsquic_stream_write(stream,c->out,c->out_len);
        if(n>0){memmove(c->out,c->out+n,c->out_len-(size_t)n);c->out_len-=(size_t)n;c->last_tx=now_us(CLOCK_MONOTONIC);lsquic_stream_flush(stream);}
        else if(n<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK)c->failed=1;
    }
    if(!c->out_len)lsquic_stream_wantwrite(stream,0);
}
static void read_stream(lsquic_stream_t *stream,lsquic_stream_ctx_t *h){
    struct context *c=(struct context*)h;if(!c)return;
    ssize_t n=lsquic_stream_read(stream,c->in+c->in_len,sizeof(c->in)-c->in_len);
    if(n<0){if(errno!=EAGAIN&&errno!=EWOULDBLOCK)c->failed=1;return;}
    if(n==0){c->failed=1;return;}c->in_len+=(size_t)n;size_t off=0;
    while(c->in_len-off>=2){
        size_t length=0,mult=1,i=1;int complete=0;
        while(i<c->in_len-off&&i<=4){unsigned char b=c->in[off+i++];length+=(b&127)*mult;mult*=128;if(!(b&128)){complete=1;break;}}
        if(!complete){if(i>4)c->failed=1;break;}
        if(length>sizeof(c->in)-i){c->failed=1;break;}
        if(off+i+length>c->in_len)break;
        packet(c,c->in[off],c->in+off+i,length);off+=i+length;
    }
    if(off){memmove(c->in,c->in+off,c->in_len-off);c->in_len-=off;}
    if(c->in_len==sizeof(c->in))c->failed=1;
}
static void stream_closed(lsquic_stream_t *stream,lsquic_stream_ctx_t *h){(void)stream;if(h)((struct context*)h)->stream=NULL;}
static const struct lsquic_stream_if callbacks={.on_new_conn=new_conn,.on_hsk_done=handshake,.on_conn_closed=conn_closed,.on_new_stream=new_stream,.on_read=read_stream,.on_write=write_stream,.on_close=stream_closed};
int main(int argc,char **argv){
    if(argc!=6&&argc!=8){fprintf(stderr,"usage: quic-client IPv4 run_id count period_ms timeout_s [--sub topic]\n");return 2;}
    struct context c={0};active=&c;c.run=argv[2];c.count=positive_number(argv[3],1);c.period=positive_number(argv[4],0);c.timeout=positive_number(argv[5],0);c.sub=argc==8;c.topic=c.sub?argv[7]:"quic4iot/pi01/data";
    if(!valid_run(c.run)||c.count<0||c.period<1||c.timeout<1||strlen(c.topic)>200||(c.sub&&strcmp(argv[6],"--sub")))return 2;
    c.identity=env_default("MQTT_SERVER_NAME",argv[1]);c.peer.sin_family=AF_INET;c.peer.sin_port=htons(14567);
    if(inet_pton(AF_INET,argv[1],&c.peer.sin_addr)!=1){fprintf(stderr,"QUIC network address must be IPv4\n");return 2;}
    setvbuf(stdout,NULL,_IOLBF,0);signal(SIGINT,stop);signal(SIGTERM,stop);
    c.ssl=SSL_CTX_new(TLS_method());const char *ca=env_default("MQTT_CA_FILE","../emqx/certs/ca.crt");
    if(!c.ssl||!SSL_CTX_set_min_proto_version(c.ssl,TLS1_3_VERSION)||!SSL_CTX_set_max_proto_version(c.ssl,TLS1_3_VERSION)||!SSL_CTX_load_verify_locations(c.ssl,ca,NULL)||!SSL_CTX_set1_curves_list(c.ssl,"X25519")){fprintf(stderr,"QUIC TLS/CA setup failed\n");return 2;}
    const uint16_t cipher_ids[]={0x1301}; /* TLS_AES_128_GCM_SHA256, RFC 8446 */
    if(!SSL_CTX_set1_tls13_ciphers(c.ssl,cipher_ids,NULL,1)){fprintf(stderr,"QUIC TLS 1.3 cipher restriction failed\n");return 2;}
    SSL_CTX_set_verify(c.ssl,SSL_VERIFY_PEER,NULL);SSL_CTX_set_info_callback(c.ssl,ssl_info);SSL_CTX_set_keylog_callback(c.ssl,keylog);SSL_CTX_set_session_cache_mode(c.ssl,SSL_SESS_CACHE_OFF);
    unsigned char ip[16];int is_ip=inet_pton(AF_INET,c.identity,ip)==1||inet_pton(AF_INET6,c.identity,ip)==1;
    if(!(is_ip?X509_VERIFY_PARAM_set1_ip_asc(SSL_CTX_get0_param(c.ssl),c.identity):X509_VERIFY_PARAM_set1_host(SSL_CTX_get0_param(c.ssl),c.identity,strlen(c.identity)))){fprintf(stderr,"Certificate identity configuration failed\n");return 2;}
    const unsigned char alpn[]={4,'m','q','t','t'};if(SSL_CTX_set_alpn_protos(c.ssl,alpn,sizeof alpn)){fprintf(stderr,"ALPN setup failed\n");return 2;}
    lsquic_logger_init(&logger,NULL,LLTS_HHMMSSMS);lsquic_set_log_level("warn");
    if(lsquic_global_init(LSQUIC_GLOBAL_CLIENT)){fprintf(stderr,"LSQUIC global init failed\n");return 2;}
    c.fd=socket(AF_INET,SOCK_DGRAM,0);if(c.fd<0)return 2;
    if(connect(c.fd,(struct sockaddr*)&c.peer,sizeof c.peer)<0){perror("UDP connect");return 2;}
    socklen_t size=sizeof c.local;if(getsockname(c.fd,(struct sockaddr*)&c.local,&size))return 2;
    if(fcntl(c.fd,F_SETFL,fcntl(c.fd,F_GETFL)|O_NONBLOCK)<0)return 2;
    struct lsquic_engine_settings settings;lsquic_engine_init_settings(&settings,0);settings.es_versions=1<<LSQVER_I001;settings.es_idle_timeout=45;
    struct lsquic_engine_api api={0};api.ea_settings=&settings;api.ea_packets_out=packets_out;api.ea_packets_out_ctx=&c;api.ea_stream_if=&callbacks;api.ea_stream_if_ctx=&c;api.ea_alpn="mqtt";api.ea_get_ssl_ctx=get_ssl;
    c.engine=lsquic_engine_new(0,&api);if(!c.engine){fprintf(stderr,"LSQUIC engine creation failed\n");return 2;}
    c.running=1;c.start_wall=now_us(CLOCK_REALTIME);c.start=now_us(CLOCK_MONOTONIC);c.last_tx=c.start;
    c.conn=lsquic_engine_connect(c.engine,LSQVER_I001,(struct sockaddr*)&c.local,(struct sockaddr*)&c.peer,&c,NULL,c.identity,0,NULL,0,NULL,0);
    if(!c.conn)c.failed=1;
    while(c.running&&!c.failed&&!c.completed&&!stopped){
        uint64_t now=now_us(CLOCK_MONOTONIC);
        if(!c.ready&&now-c.start>(uint64_t)c.timeout*1000000){c.failed=1;break;}
        if(c.ready&&!c.sub&&c.seq<c.count&&now>=c.next_send){
            char payload[129];uint64_t ts=now_us(CLOCK_REALTIME);if(make_payload(payload,c.run,c.seq,ts)){c.failed=1;break;}
            unsigned char body[512];size_t n=put_string(body,c.topic);memcpy(body+n,payload,128);
            int rc=queue_packet(&c,0x30,body,n+128);
            printf("{\"event\":\"send\",\"seq\":%d,\"ts_us\":%" PRIu64 ",\"rc\":%d}\n",c.seq,ts,rc);
            if(rc)break;
            c.seq++;c.last_send=now;c.next_send=c.connack+2000000+(uint64_t)c.seq*c.period*1000;
        }
        if(c.ready&&!c.sub&&c.seq==c.count&&c.last_send&&now-c.last_send>=10000000){c.completed=1;break;}
        if(c.ready&&now-c.last_tx>=15000000){queue_packet(&c,0xC0,NULL,0);c.last_tx=now;}
        lsquic_engine_process_conns(c.engine);if(lsquic_engine_has_unsent_packets(c.engine))lsquic_engine_send_unsent_packets(c.engine);
        fd_set set;FD_ZERO(&set);FD_SET(c.fd,&set);struct timeval tv={0,1000};int diff;
        if(lsquic_engine_earliest_adv_tick(c.engine,&diff)&&diff<1000){tv.tv_usec=diff>0?diff:0;}
        if(select(c.fd+1,&set,NULL,NULL,&tv)>0){
            for(;;){unsigned char buf[65536];ssize_t n=recv(c.fd,buf,sizeof buf,0);if(n<0){if(errno!=EAGAIN&&errno!=EWOULDBLOCK)c.failed=1;break;}
                lsquic_engine_packet_in(c.engine,buf,(size_t)n,(struct sockaddr*)&c.local,(struct sockaddr*)&c.peer,&c,0);
            }
        }
    }
    if(!c.connect_logged)log_connect(c.run,"quic",0,0,c.start_wall,0);
    if(c.sub&&stopped&&c.subscribed&&!c.failed)c.completed=1;
    int outcome=!c.completed||c.failed;
    printf("{\"event\":\"end\",\"success\":%s,\"sent\":%d,\"disconnected\":%s}\n",outcome?"false":"true",c.seq,(!c.running&&!c.completed)?"true":"false");
    if(c.conn&&c.stream){queue_packet(&c,0xE0,NULL,0);lsquic_engine_process_conns(c.engine);}
    lsquic_engine_destroy(c.engine);close(c.fd);SSL_CTX_free(c.ssl);lsquic_global_cleanup();return outcome?1:0;
}
