(() => {
  const progress = document.querySelector("#bindingProgress");
  const guide = document.querySelector("#browserGuide");
  const errorPanel = document.querySelector("#bindingError");
  const errorText = document.querySelector("#bindingErrorText");
  const copyButton = document.querySelector("#copyLink");
  const copyResult = document.querySelector("#copyResult");
  const ua = navigator.userAgent || "";

  const inAppPatterns = [
    /MicroMessenger/i,
    /\bQQ\//i,
    /MQQBrowser/i,
    /AlipayClient/i,
    /Weibo/i,
    /DingTalk/i,
    /Lark/i,
    /Feishu/i,
    /BytedanceWebview/i,
    /Toutiao/i,
    /Aweme/i,
  ];

  if (inAppPatterns.some((pattern) => pattern.test(ua))) {
    progress.hidden = true;
    guide.hidden = false;
    copyButton.addEventListener("click", copyCurrentLink);
    return;
  }

  bind().catch((error) => {
    progress.hidden = true;
    errorPanel.hidden = false;
    errorText.textContent = error.message || "绑定失败，请重新扫描二维码。";
  });

  async function bind() {
    const token = decodeURIComponent(location.pathname.split("/").pop() || "");
    const metadata = await detectDevice();
    const response = await fetch("/api/pair/redeem", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      body: JSON.stringify({token, metadata}),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || "二维码可能已过期或已被使用。");
    }
    location.replace("/");
  }

  async function detectDevice() {
    const platform = detectPlatform(ua);
    const metadata = {
      platform,
      os: detectOS(ua, platform),
      browser: detectBrowser(ua),
      model: detectModel(ua, platform),
      form_factor: matchMedia("(pointer: coarse)").matches ? "触屏设备" : "电脑",
      screen: `${screen.width}×${screen.height}@${window.devicePixelRatio || 1}`,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
      language: navigator.language || "",
      user_agent: ua.slice(0, 500),
    };

    if (navigator.userAgentData?.getHighEntropyValues) {
      try {
        const details = await navigator.userAgentData.getHighEntropyValues([
          "model",
          "platformVersion",
          "fullVersionList",
        ]);
        if (details.model) metadata.model = details.model;
        if (navigator.userAgentData.platform) metadata.platform = navigator.userAgentData.platform;
        if (details.platformVersion) {
          metadata.os = `${metadata.platform} ${details.platformVersion}`.trim();
        }
        const browser = details.fullVersionList?.find((item) =>
          /Edge|Chrome|Chromium|Opera/i.test(item.brand)
        );
        if (browser) metadata.browser = `${browser.brand} ${browser.version}`;
      } catch {
        // User-Agent Client Hints are optional; the fallback above is sufficient.
      }
    }
    return metadata;
  }

  function detectPlatform(value) {
    if (/iPad/i.test(value) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iPad";
    if (/iPhone/i.test(value)) return "iPhone";
    if (/Android/i.test(value)) return "Android";
    if (/Windows/i.test(value)) return "Windows";
    if (/Macintosh|Mac OS X/i.test(value)) return "macOS";
    return navigator.platform || "未知设备";
  }

  function detectOS(value, platform) {
    if (platform === "iPhone" || platform === "iPad") {
      const match = value.match(/OS ([\d_]+)/);
      return match ? `iOS ${match[1].replaceAll("_", ".")}` : "iOS";
    }
    if (platform === "Android") {
      const match = value.match(/Android\s+([\d.]+)/);
      return match ? `Android ${match[1]}` : "Android";
    }
    if (platform === "Windows") return "Windows";
    if (platform === "macOS") {
      const match = value.match(/Mac OS X\s+([\d_]+)/);
      return match ? `macOS ${match[1].replaceAll("_", ".")}` : "macOS";
    }
    return platform;
  }

  function detectBrowser(value) {
    const candidates = [
      ["Edge", /Edg(?:A|iOS)?\/([\d.]+)/],
      ["Chrome", /(?:Chrome|CriOS)\/([\d.]+)/],
      ["Firefox", /(?:Firefox|FxiOS)\/([\d.]+)/],
      ["Safari", /Version\/([\d.]+).*Safari/],
    ];
    for (const [name, pattern] of candidates) {
      const match = value.match(pattern);
      if (match) return `${name} ${match[1]}`;
    }
    return "未知浏览器";
  }

  function detectModel(value, platform) {
    if (platform === "Android") {
      const build = value.match(/;\s*([^;()]+?)\s+Build\//i);
      if (build) return build[1].trim();
      const fallback = value.match(/Android[^;]*;\s*([^;)]+)/i);
      if (fallback) return fallback[1].replace(/\s+wv$/i, "").trim();
    }
    if (platform === "iPhone" || platform === "iPad") return platform;
    return "";
  }

  async function copyCurrentLink() {
    try {
      await navigator.clipboard.writeText(location.href);
      copyResult.textContent = "链接已复制，请粘贴到系统浏览器。";
    } catch {
      const input = document.createElement("textarea");
      input.value = location.href;
      input.setAttribute("readonly", "");
      document.body.append(input);
      input.select();
      document.execCommand("copy");
      input.remove();
      copyResult.textContent = "链接已复制，请粘贴到系统浏览器。";
    }
  }
})();
