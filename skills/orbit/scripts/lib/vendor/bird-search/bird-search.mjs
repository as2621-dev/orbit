#!/usr/bin/env node
/**
 * bird-search.mjs - Vendored Bird CLI search wrapper for /last30days.
 * Subset of @steipete/bird v0.8.0 (MIT License, Peter Steinberger).
 *
 * Usage:
 *   node bird-search.mjs <query> [--count N] [--json]
 *   node bird-search.mjs --whoami
 *   node bird-search.mjs --check
 */

import { resolveCredentials } from './lib/cookies.js';
import { TwitterClientBase } from './lib/twitter-client-base.js';
import { withFollowing } from './lib/twitter-client-following.js';
import { withSearch } from './lib/twitter-client-search.js';

// Build a read-only client (search + following; no posting, bookmarks, etc.)
const SearchClient = withFollowing(withSearch(TwitterClientBase));

const args = process.argv.slice(2);

function writeStdout(text) {
  if (text) process.stdout.write(text);
}

function writeStderr(text) {
  if (text) process.stderr.write(text);
}

async function main() {
  // --check: verify that credentials can be resolved
  if (args.includes('--check')) {
    try {
      const { cookies, warnings } = await resolveCredentials({});
      if (cookies.authToken && cookies.ct0) {
        writeStdout(JSON.stringify({ authenticated: true, source: cookies.source }));
        return 0;
      }
      writeStdout(JSON.stringify({ authenticated: false, warnings }));
      return 1;
    } catch (err) {
      writeStdout(JSON.stringify({ authenticated: false, error: err.message }));
      return 1;
    }
  }

  // --whoami: check auth and output source
  if (args.includes('--whoami')) {
    try {
      const { cookies } = await resolveCredentials({});
      if (cookies.authToken && cookies.ct0) {
        writeStdout(cookies.source || 'authenticated');
        return 0;
      }
      writeStderr('Not authenticated\n');
      return 1;
    } catch (err) {
      writeStderr(`Auth check failed: ${err.message}\n`);
      return 1;
    }
  }

  // --following <screen_name|userId> [--count N] [--json]
  // The Following op needs a numeric userId; a numeric arg is used directly.
  // screen_name -> id resolution is a runtime concern (Python wrapper / --whoami).
  const followingIdx = args.indexOf('--following');
  const followingUserIdIdx = args.indexOf('--following-user-id');
  if (followingIdx !== -1 || followingUserIdIdx !== -1) {
    let followTarget = null;
    if (followingUserIdIdx !== -1 && args[followingUserIdIdx + 1]) {
      followTarget = args[followingUserIdIdx + 1];
    } else if (followingIdx !== -1 && args[followingIdx + 1] && !args[followingIdx + 1].startsWith('-')) {
      followTarget = args[followingIdx + 1];
    }

    let followCount = Number.POSITIVE_INFINITY;
    let followJson = false;
    for (let i = 0; i < args.length; i++) {
      if ((args[i] === '--count' || args[i] === '-n') && args[i + 1]) {
        followCount = parseInt(args[i + 1], 10);
        i++;
      } else if (args[i] === '--json') {
        followJson = true;
      }
    }

    if (!followTarget) {
      writeStderr('Usage: node bird-search.mjs --following <userId> [--count N] [--json]\n');
      return 1;
    }

    try {
      const { cookies, warnings } = await resolveCredentials({});
      if (!cookies.authToken || !cookies.ct0) {
        const msg = warnings.length > 0 ? warnings.join('; ') : 'No Twitter credentials found';
        if (followJson) {
          writeStdout(JSON.stringify({ error: msg, items: [] }));
        } else {
          writeStderr(`Error: ${msg}\n`);
        }
        return 1;
      }

      const client = new SearchClient({
        cookies: {
          authToken: cookies.authToken,
          ct0: cookies.ct0,
          cookieHeader: cookies.cookieHeader,
        },
        timeoutMs: 30000,
      });

      const result = await client.following(followTarget, {
        limit: Number.isFinite(followCount) ? followCount : undefined,
      });

      if (!result.success) {
        if (followJson) {
          writeStdout(JSON.stringify({ error: result.error, items: [] }));
        } else {
          writeStderr(`Following failed: ${result.error}\n`);
        }
        return 1;
      }

      const follows = (result.users || []).map((user) => ({
        creator_handle: user.username,
        display_name: user.name,
        rest_id: user.id,
      }));
      if (followJson) {
        writeStdout(JSON.stringify(follows));
      } else {
        for (const follow of follows) {
          writeStdout(`@${follow.creator_handle} (${follow.display_name})\n`);
        }
      }
      return 0;
    } catch (err) {
      if (followJson) {
        writeStdout(JSON.stringify({ error: err.message, items: [] }));
      } else {
        writeStderr(`Error: ${err.message}\n`);
      }
      return 1;
    }
  }

  // Parse search args
  let query = null;
  let count = 20;
  let jsonOutput = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--count' && args[i + 1]) {
      count = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i] === '-n' && args[i + 1]) {
      count = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i] === '--json') {
      jsonOutput = true;
    } else if (!args[i].startsWith('-')) {
      query = args[i];
    }
  }

  if (!query) {
    writeStderr('Usage: node bird-search.mjs <query> [--count N] [--json]\n');
    return 1;
  }

  try {
    // Resolve credentials (env vars, then browser cookies)
    const { cookies, warnings } = await resolveCredentials({});

    if (!cookies.authToken || !cookies.ct0) {
      const msg = warnings.length > 0 ? warnings.join('; ') : 'No Twitter credentials found';
      if (jsonOutput) {
        writeStdout(JSON.stringify({ error: msg, items: [] }));
      } else {
        writeStderr(`Error: ${msg}\n`);
      }
      return 1;
    }

    const client = new SearchClient({
      cookies: {
        authToken: cookies.authToken,
        ct0: cookies.ct0,
        cookieHeader: cookies.cookieHeader,
      },
      timeoutMs: 30000,
    });

    const result = await client.search(query, count);

    if (!result.success) {
      if (jsonOutput) {
        writeStdout(JSON.stringify({ error: result.error, items: [] }));
      } else {
        writeStderr(`Search failed: ${result.error}\n`);
      }
      return 1;
    }

    const tweets = result.tweets || [];
    if (jsonOutput) {
      writeStdout(JSON.stringify(tweets));
    } else {
      for (const tweet of tweets) {
        const author = tweet.author?.username || 'unknown';
        writeStdout(`@${author}: ${tweet.text?.slice(0, 200)}\n\n`);
      }
    }

    return 0;
  } catch (err) {
    if (jsonOutput) {
      writeStdout(JSON.stringify({ error: err.message, items: [] }));
    } else {
      writeStderr(`Error: ${err.message}\n`);
    }
    return 1;
  }
}

try {
  const code = await main();
  process.exitCode = Number.isInteger(code) ? code : 1;
} catch (err) {
  writeStderr(`Fatal error: ${err?.message || err}\n`);
  process.exitCode = 1;
}
