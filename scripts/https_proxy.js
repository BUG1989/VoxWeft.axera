#!/usr/bin/env node
/**
 * Small HTTPS reverse proxy for LAN demos.
 *
 * The VoxWeft runtime itself stays on plain HTTP. This proxy terminates TLS on
 * a high port and forwards both normal HTTP requests and WebSocket upgrades to
 * the local runtime. It is intentionally dependency-free.
 */
'use strict';

const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const net = require('node:net');

const listenHost = process.env.VOXWEFT_HTTPS_HOST || '0.0.0.0';
const listenPort = Number(process.env.VOXWEFT_HTTPS_PORT || 3443);
const backendHost = process.env.VOXWEFT_BACKEND_HOST || '127.0.0.1';
const backendPort = Number(process.env.VOXWEFT_BACKEND_PORT || 3100);
const certFile = process.env.VOXWEFT_TLS_CERT || `${__dirname}/../.certs/voxweft-axera.crt`;
const keyFile = process.env.VOXWEFT_TLS_KEY || `${__dirname}/../.certs/voxweft-axera.key`;

const certificate = fs.readFileSync(certFile);
const privateKey = fs.readFileSync(keyFile);

const server = https.createServer({ cert: certificate, key: privateKey }, (req, res) => {
  const headers = { ...req.headers };
  headers.host = `${backendHost}:${backendPort}`;
  headers['x-forwarded-host'] = req.headers.host || '';
  headers['x-forwarded-proto'] = 'https';

  const upstream = http.request({
    hostname: backendHost,
    port: backendPort,
    method: req.method,
    path: req.url,
    headers,
  }, upstreamRes => {
    res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
    upstreamRes.pipe(res);
  });

  upstream.on('error', error => {
    if (!res.headersSent) res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end(`VoxWeft HTTPS proxy backend error: ${error.message}\n`);
  });

  req.pipe(upstream);
});

server.on('upgrade', (req, socket, head) => {
  socket.setTimeout(0);

  const upstream = net.connect(backendPort, backendHost, () => {
    const forwarded = [];
    for (const [name, value] of Object.entries(req.headers)) {
      if (Array.isArray(value)) {
        for (const item of value) forwarded.push(`${name}: ${item}`);
      } else if (value !== undefined) {
        forwarded.push(`${name}: ${value}`);
      }
    }

    const request = [
      `${req.method} ${req.url} HTTP/1.1`,
      `Host: ${backendHost}:${backendPort}`,
      ...forwarded.filter(line => !/^host:/i.test(line)),
      'X-Forwarded-Proto: https',
      '',
      '',
    ].join('\r\n');

    upstream.write(request);
    if (head?.length) upstream.write(head);
    socket.pipe(upstream);
    upstream.pipe(socket);
  });

  const fail = error => {
    socket.destroy(error);
    upstream.destroy(error);
  };
  socket.on('error', fail);
  upstream.on('error', fail);
});

server.listen(listenPort, listenHost, () => {
  console.log(`VoxWeft HTTPS proxy: https://${listenHost}:${listenPort} -> http://${backendHost}:${backendPort}`);
  console.log(`Certificate: ${certFile}`);
});

const shutdown = () => server.close(() => process.exit(0));
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
