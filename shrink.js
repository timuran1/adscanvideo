// Shrinks a video on the visitor's own device so it fits the upload limit.
// Uses the browser's built-in encoder (WebCodecs) through Mediabunny, which is
// loaded only when a file is actually too large. The original file is never changed.
(() => {
  const VENDOR = '/vendor/mediabunny-1.66.0.min.js';
  const TARGET_BYTES = 90 * 1000 * 1000; // leaves headroom under the 100 MB limit
  let loading = null;
  function load() {
    loading = loading || new Promise((resolve, reject) => {
      const script = Object.assign(document.createElement('script'), { src: VENDOR });
      script.onload = () => window.Mediabunny ? resolve(window.Mediabunny) : reject(new Error('load'));
      script.onerror = () => { loading = null; reject(new Error('load')); };
      document.head.append(script);
    });
    return loading;
  }
  // Pick picture size, frame rate and bitrates that fit the target for this length.
  function plan(seconds) {
    const total = TARGET_BYTES * 8 / seconds * 0.95; // 5% for container overhead
    const audio = total < 400e3 ? 48e3 : total < 1e6 ? 64e3 : 96e3;
    const video = Math.min(3e6, total - audio);
    return { audio, video, mono: total < 400e3, shortSide: video >= 1.6e6 ? 720 : video >= 600e3 ? 480 : 360, fps: video >= 500e3 ? 30 : 15 };
  }
  function fail(code) { return Object.assign(new Error(code), { code }); }

  window.adscanShrink = {
    supported() { return typeof VideoEncoder === 'function' && typeof VideoDecoder === 'function'; },
    plan,
    // Resolves with a new File under the limit. onProgress gets a number from 0 to 1.
    async run(file, onProgress) {
      let MB;
      try { MB = await load(); } catch { throw fail('load_failed'); }
      const input = new MB.Input({ formats: MB.ALL_FORMATS, source: new MB.BlobSource(file) });
      try {
        let seconds;
        try { seconds = await input.computeDuration(); } catch { throw fail('unreadable'); }
        if (!(seconds > 0)) throw fail('unreadable');
        const settings = plan(seconds);
        if (settings.video < 100e3) throw fail('too_long');
        const output = new MB.Output({ format: new MB.Mp4OutputFormat({ fastStart: 'in-memory' }), target: new MB.BufferTarget() });
        const conversion = await MB.Conversion.init({
          input, output, tracks: 'primary',
          video: async track => {
            const width = await track.getDisplayWidth(), height = await track.getDisplayHeight();
            const scale = Math.min(1, settings.shortSide / Math.min(width, height));
            // Encoders want even dimensions.
            return { width: Math.max(2, Math.round(width * scale / 2) * 2), frameRate: settings.fps, quality: new MB.Quality({ bitrate: settings.video, bitrateMode: 'variable' }), forceTranscode: true };
          },
          audio: { quality: new MB.Quality({ bitrate: settings.audio }), numberOfChannels: settings.mono ? 1 : 2, forceTranscode: true }
        });
        const lostVideo = conversion.discardedTracks.some(item => item.track.type === 'video' && item.reason !== 'discarded_by_user');
        if (!conversion.isValid || lostVideo) throw fail('unsupported_video');
        if (onProgress) conversion.onProgress = onProgress;
        try { await conversion.execute(); } catch { throw fail('encode_failed'); }
        const buffer = output.target.buffer;
        if (!buffer || buffer.byteLength > 100 * 1000 * 1000) throw fail('still_too_large');
        return new File([buffer], 'shrunk-video.mp4', { type: 'video/mp4' });
      } finally { try { input.dispose(); } catch {} }
    }
  };
})();
