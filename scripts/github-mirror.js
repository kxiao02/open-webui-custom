const prefix = process.env.GITHUB_MIRROR_PREFIX;
const originalFetch = globalThis.fetch;

if (prefix && typeof originalFetch === "function") {
  const normalizedPrefix = prefix.replace(/\/$/, "");
  const githubPrefix = "https://github.com/";

  globalThis.fetch = (input, init) => {
    if (typeof input === "string" && input.startsWith(githubPrefix)) {
      const rest = input.slice(githubPrefix.length);
      const mirrored = `${normalizedPrefix}/${rest}`;
      return originalFetch(mirrored, init);
    }
    return originalFetch(input, init);
  };
}
