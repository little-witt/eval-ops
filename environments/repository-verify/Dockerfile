FROM alpine/git:latest

USER root
RUN addgroup -g 10001 aceval \
    && adduser -D -u 10001 -G aceval aceval

COPY repository_verify.sh /opt/aceval/repository_verify.sh
RUN chmod 0555 /opt/aceval/repository_verify.sh

USER aceval
WORKDIR /workspace

ENTRYPOINT []
CMD ["tail", "-f", "/dev/null"]
