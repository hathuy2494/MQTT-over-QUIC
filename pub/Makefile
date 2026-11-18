CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11
DEPS ?= $(CURDIR)/deps
LSQUIC_DIR ?= $(DEPS)/lsquic
BORINGSSL_DIR ?= $(DEPS)/boringssl
LSQUIC_BUILD_DIR ?= $(LSQUIC_DIR)/build
BORINGSSL_BUILD_DIR ?= $(BORINGSSL_DIR)/build
BORINGSSL_SSL_LIB ?= $(firstword $(wildcard $(BORINGSSL_BUILD_DIR)/libssl.a $(BORINGSSL_BUILD_DIR)/ssl/libssl.a))
BORINGSSL_CRYPTO_LIB ?= $(firstword $(wildcard $(BORINGSSL_BUILD_DIR)/libcrypto.a $(BORINGSSL_BUILD_DIR)/crypto/libcrypto.a))
.PHONY: all tcp quic clean wrappers
all: wrappers tcp quic
wrappers:
	chmod +x bench-pub bench-sub build-deps.sh
tcp: tcp-pub
quic: quic-client
tcp-pub: tcp_pub.c bench_common.h
	$(CC) $(CFLAGS) tcp_pub.c -o $@ -lmosquitto -lssl -lcrypto
quic-client: quic_client.c bench_common.h
	test -n "$(BORINGSSL_SSL_LIB)" -a -n "$(BORINGSSL_CRYPTO_LIB)"
	$(CC) $(CFLAGS) -I$(LSQUIC_DIR)/include -I$(BORINGSSL_DIR)/include quic_client.c -o $@ $(LSQUIC_BUILD_DIR)/src/liblsquic/liblsquic.a $(BORINGSSL_SSL_LIB) $(BORINGSSL_CRYPTO_LIB) -lz -lm -pthread -lstdc++
clean:
	rm -f tcp-pub quic-client
