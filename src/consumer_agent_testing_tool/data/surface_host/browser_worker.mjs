// Trusted coordinator transport for an offline Chromium worker.
// No worker-management operation is included in the model-visible MCP catalog.
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

export class ContainerBrowser {
  constructor(root, config, observe = () => {}) {
    this.root = fs.realpathSync(root);
    this.config = config;
    this.observe = observe;
    this.name = 'surface-browser-' + crypto.randomUUID();
    this.owner = crypto.randomUUID();
    this.bridges = new Set();
    this.sockets = new Set();
    this.closed = false;
    if (!/^sha256:[a-f0-9]{64}$/.test(config.image || ''))
      throw Error('Browser worker requires an immutable local image ID');
    this.lifetime = config.lifetime_seconds ?? 240;
    if (!Number.isInteger(this.lifetime) || this.lifetime < 30 || this.lifetime > 3900)
      throw Error('Invalid browser worker lifetime');
    if (config.probe_loopback_webrtc !== undefined && typeof config.probe_loopback_webrtc !== 'boolean')
      throw Error('Invalid trusted WebRTC probe setting');
    const approved = JSON.parse(process.env.CONSUMER_AGENT_TESTING_TOOL_WORKER_DOCKER || '[]');
    const selected = approved.some(row => row.socket === config.docker_socket &&
      row.config === config.docker_config && row.image === config.image);
    const inside = value => {
      if (typeof value !== 'string') throw Error('Missing browser worker path');
      const resolved = fs.realpathSync(path.resolve(this.root, value));
      if (selected ? resolved !== value : !resolved.startsWith(this.root + path.sep))
        throw Error('Worker path escapes its selected connection');
      return resolved;
    };
    this.socket = inside(config.docker_socket);
    this.dockerConfig = inside(config.docker_config);
    if (!fs.statSync(this.socket).isSocket() || !fs.statSync(this.dockerConfig).isDirectory())
      throw Error('Worker requires an explicit Docker socket and config directory');
    this.seccomp = fileURLToPath(new URL('../browser_worker/seccomp_profile.worker.json', import.meta.url));
    const home = path.join(this.root, '.cache', 'browser-worker-host');
    fs.mkdirSync(home, {recursive: true, mode: 0o700});
    this.env = {PATH: process.env.PATH, HOME: home, TMPDIR: home,
      DOCKER_CONFIG: this.dockerConfig, DOCKER_HOST: 'unix://' + this.socket};
  }
  command(args, extra = {}) {
    return spawn('docker', ['--config', this.dockerConfig, '--host', 'unix://' + this.socket, ...args],
      {cwd: this.root, env: this.env, stdio: ['ignore', 'pipe', 'pipe'], ...extra});
  }
  async run(args, timeout = 30000) {
    const child = this.command(args);
    let stdout = '', stderr = '';
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout);
    return await new Promise((resolve, reject) => {
      child.stdout.on('data', bytes => {
        stdout += bytes.toString();
        if (stdout.length > 1024 * 1024) child.kill('SIGKILL');
      });
      child.stderr.on('data', bytes => {
        stderr += bytes.toString();
        if (stderr.length > 1024 * 1024) child.kill('SIGKILL');
      });
      child.on('error', reject);
      child.on('close', code => {
        clearTimeout(timer);
        if (code !== 0) reject(Error(`Browser worker command failed (${code}): ${stderr.slice(0, 2000)}`));
        else resolve((stdout + (args[0] === 'logs' ? stderr : '')).trim());
      });
    });
  }
  async launch(chromium) {
    try {
      await this.run(['run', '-d', '--rm', '--name', this.name,
        '--label', 'consumer-agent-testing-tool.browser-worker=1', '--label', 'consumer-agent-testing-tool.owner=' + this.owner,
        '--pull=never', '--network=none', '--dns=127.0.0.1', '--ipc=private', '--read-only',
        '--cap-drop=ALL', '--cap-add=NET_ADMIN', '--cap-add=CHOWN',
        '--cap-add=SETUID', '--cap-add=SETGID', '--cap-add=SETPCAP', '--cap-add=DAC_READ_SEARCH',
        '--security-opt=no-new-privileges', '--security-opt=seccomp=' + this.seccomp,
        '--memory=1g', '--cpus=1', '--pids-limit=256', '--shm-size=128m',
        '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777',
        '--tmpfs=/control:rw,nosuid,nodev,noexec,size=1m,mode=0700',
        '--env=SURFACE_BROWSER_LIFETIME_SECONDS=' + this.lifetime,
        ...(this.config.probe_loopback_webrtc ? ['--env=SURFACE_BROWSER_PROBE_LOOPBACK_WEBRTC=1'] : []),
        this.config.image], 60000);
      const deadline = Date.now() + 45000;
      let ready;
      while (Date.now() < deadline) {
        const running = await this.run(['inspect', '--format', '{{.State.Running}}', this.name]);
        if (running !== 'true')
          throw Error('Browser worker exited before readiness: ' +
            await this.run(['logs', '--tail=30', this.name]).catch(() => 'logs unavailable'));
        try {
          ready = JSON.parse(await this.run(['exec', '--user=0', this.name,
            'cat', '/control/ready.json'], 5000));
          break;
        } catch { await new Promise(resolve => setTimeout(resolve, 100)); }
      }
      if (!ready) throw Error('Browser worker readiness timed out');
      const endpoint = new URL(ready.endpoint);
      if (endpoint.protocol !== 'ws:' || endpoint.hostname !== '127.0.0.1' ||
          ready.uid !== 65532 || ready.network !== 'none' || !ready.chromium_sandbox)
        throw Error('Invalid browser worker readiness record');
      if (ready.webrtc_loopback_probe !== (this.config.probe_loopback_webrtc === true))
        throw Error('Worker did not confirm the selected WebRTC probe treatment; rebuild the image');
      this.ready = ready;
      const engine = JSON.parse(await this.run(['version', '--format', '{{json .Server}}']));
      this.runtime = {docker_version: engine.Version, docker_os: engine.Os,
        docker_arch: engine.Arch, kernel: await this.run([
          'exec', '--user=0', this.name, 'uname', '-srmo'])};
      if (Object.values(this.runtime).some(value => typeof value !== 'string' || !value.trim()))
        throw Error('Docker worker runtime identity is incomplete');
      this.relay = net.createServer(socket => {
        this.sockets.add(socket);
        const child = this.command(['exec', '-i', '--user=0', this.name,
          'node', '/opt/worker/relay.mjs'], {stdio: ['pipe', 'pipe', 'pipe']});
        this.bridges.add(child);
        socket.pipe(child.stdin);
        child.stdout.pipe(socket);
        child.stderr.resume();
        child.stdin.on('error', () => socket.destroy());
        child.on('error', () => socket.destroy());
        child.on('close', () => {this.bridges.delete(child); socket.destroy();});
        socket.on('error', () => child.kill('SIGKILL'));
        socket.on('close', () => {this.sockets.delete(socket); child.kill('SIGKILL');});
      });
      await new Promise((resolve, reject) => {
        this.relay.once('error', reject);
        this.relay.listen(0, '127.0.0.1', resolve);
      });
      endpoint.port = String(this.relay.address().port);
      this.browser = await chromium.connect(endpoint.href, {timeout: 30000});
      this.observe({kind: 'browser_worker', network: 'none', image: this.config.image,
        chromium_sandbox: true, uid: 65532, os_isolation: 'qualification-pending',
        webrtc_loopback_probe: ready.webrtc_loopback_probe === true, runtime: this.runtime});
      return this.browser;
    } catch (error) {
      try { await this.close(); }
      catch (cleanup) { throw new AggregateError([error, cleanup], 'Worker failed and cleanup failed'); }
      throw error;
    }
  }
  async close() {
    if (this.closed) return;
    // Stop the browser before destroying its private relay, then remove exactly
    // the uniquely named worker created by this instance. Cleanup must be verified.
    await this.browser?.close().catch(() => {});
    for (const socket of this.sockets) socket.destroy();
    for (const child of this.bridges) child.kill('SIGKILL');
    if (this.relay) await new Promise(resolve => this.relay.close(resolve));
    const owned = () => this.run(['ps', '-aq', '--filter', 'name=^/' + this.name + '$',
      '--filter', 'label=consumer-agent-testing-tool.owner=' + this.owner]);
    if (await owned()) {
      try { await this.run(['rm', '-f', this.name]); }
      catch (error) { if (await owned()) throw error; } // It may have expired during cleanup.
    }
    if (await owned()) throw Error('Browser worker cleanup is not confirmed');
    this.closed = true;
  }
}
