FROM mcr.microsoft.com/playwright@sha256:a0f4498920a5dbac63196d9140ed738ef00470f27e2e74029abd8850b7bd5717
COPY debs /opt/debs
RUN dpkg -i /opt/debs/*.deb && rm -rf /opt/debs
COPY node_modules /opt/worker/node_modules
COPY worker /opt/worker
RUN chmod 0555 /opt/worker/entrypoint.sh /opt/worker/chromium-wrapper.mjs \
    && chmod -R a-w /opt/worker
ENV HOME=/tmp/browser-home PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /opt/worker
ENTRYPOINT ["/opt/worker/entrypoint.sh"]
