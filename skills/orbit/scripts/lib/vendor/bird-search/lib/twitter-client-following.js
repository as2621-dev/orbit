import { TWITTER_API_BASE } from './twitter-client-constants.js';
import { buildFollowingFeatures } from './twitter-client-features.js';
import { extractCursorFromInstructions, parseUsersFromInstructions } from './twitter-client-utils.js';
const NUMERIC_ID_REGEX = /^\d+$/;
function isQueryIdMismatch(payload) {
    try {
        const parsed = JSON.parse(payload);
        return (parsed.errors?.some((error) => error?.extensions?.code === 'GRAPHQL_VALIDATION_FAILED') ?? false);
    }
    catch {
        return false;
    }
}
function extractFollowingInstructions(data) {
    // The Following timeline nests instructions under user.result.timeline.timeline.
    // Tolerate the common GraphQL shape variants and fall back gracefully.
    const userResult = data?.data?.user?.result;
    return (userResult?.timeline?.timeline?.instructions ??
        userResult?.timeline_v2?.timeline?.instructions ??
        userResult?.timeline?.instructions ??
        undefined);
}
export function withFollowing(Base) {
    class TwitterClientFollowing extends Base {
        // biome-ignore lint/complexity/noUselessConstructor lint/suspicious/noExplicitAny: TS mixin constructor requirement.
        constructor(...args) {
            super(...args);
        }
        /**
         * List accounts the given user follows.
         *
         * The Following GraphQL op requires a numeric userId. The CLI hands a
         * screen_name, but screen_name -> id resolution is a runtime concern
         * (the Python wrapper / --whoami supplies the id). When the argument is
         * numeric it is treated as the userId directly.
         */
        async following(userId, options = {}) {
            return this.followingPaged(userId, options);
        }
        async getAllFollowing(userId, options) {
            return this.followingPaged(userId, { ...options, limit: Number.POSITIVE_INFINITY });
        }
        async followingPaged(userId, options = {}) {
            if (!userId || !NUMERIC_ID_REGEX.test(String(userId))) {
                return {
                    success: false,
                    error: 'Following requires a numeric userId; resolve the screen_name to an id first (see --whoami).',
                };
            }
            const features = buildFollowingFeatures();
            const pageSize = 20;
            const limit = options.limit ?? Number.POSITIVE_INFINITY;
            const pageDelayMs = options.pageDelayMs ?? 1000;
            const { maxPages } = options;
            const seen = new Set();
            const users = [];
            let cursor = options.cursor;
            let nextCursor;
            let pagesFetched = 0;
            const fetchPage = async (pageCount, pageCursor) => {
                let lastError;
                let had404 = false;
                const queryIds = await this.getFollowingQueryIds();
                for (const queryId of queryIds) {
                    const variables = {
                        userId: String(userId),
                        count: pageCount,
                        includePromotedContent: false,
                        ...(pageCursor ? { cursor: pageCursor } : {}),
                    };
                    const params = new URLSearchParams({
                        variables: JSON.stringify(variables),
                        features: JSON.stringify(features),
                    });
                    const url = `${TWITTER_API_BASE}/${queryId}/Following?${params.toString()}`;
                    try {
                        const response = await this.fetchWithTimeout(url, {
                            method: 'POST',
                            headers: this.getHeaders(),
                            body: JSON.stringify({ features, queryId }),
                        });
                        if (response.status === 404) {
                            had404 = true;
                            lastError = `HTTP ${response.status}`;
                            continue;
                        }
                        if (!response.ok) {
                            const text = await response.text();
                            const shouldRefreshQueryIds = (response.status === 400 || response.status === 422) && isQueryIdMismatch(text);
                            return {
                                success: false,
                                error: `HTTP ${response.status}: ${text.slice(0, 200)}`,
                                had404: had404 || shouldRefreshQueryIds,
                            };
                        }
                        const data = (await response.json());
                        if (data.errors && data.errors.length > 0) {
                            const shouldRefreshQueryIds = data.errors.some((error) => error?.extensions?.code === 'GRAPHQL_VALIDATION_FAILED');
                            return {
                                success: false,
                                error: data.errors.map((e) => e.message).join(', '),
                                had404: had404 || shouldRefreshQueryIds,
                            };
                        }
                        const instructions = extractFollowingInstructions(data);
                        const pageUsers = parseUsersFromInstructions(instructions);
                        const cursorValue = extractCursorFromInstructions(instructions);
                        return { success: true, users: pageUsers, cursor: cursorValue, had404 };
                    }
                    catch (error) {
                        lastError = error instanceof Error ? error.message : String(error);
                    }
                }
                return { success: false, error: lastError ?? 'Unknown error fetching following', had404 };
            };
            const fetchWithRefresh = async (pageCount, pageCursor) => {
                const firstAttempt = await fetchPage(pageCount, pageCursor);
                if (firstAttempt.success) {
                    return firstAttempt;
                }
                if (firstAttempt.had404) {
                    await this.refreshQueryIds();
                    const secondAttempt = await fetchPage(pageCount, pageCursor);
                    if (secondAttempt.success) {
                        return secondAttempt;
                    }
                    return { success: false, error: secondAttempt.error };
                }
                return { success: false, error: firstAttempt.error };
            };
            const unlimited = limit === Number.POSITIVE_INFINITY;
            while (unlimited || users.length < limit) {
                if (pagesFetched > 0 && pageDelayMs > 0) {
                    await this.sleep(pageDelayMs);
                }
                const pageCount = unlimited ? pageSize : Math.min(pageSize, limit - users.length);
                const page = await fetchWithRefresh(pageCount, cursor);
                if (!page.success) {
                    if (users.length > 0) {
                        return { success: false, error: page.error, users, nextCursor: cursor };
                    }
                    return { success: false, error: page.error };
                }
                pagesFetched += 1;
                let added = 0;
                for (const user of page.users) {
                    if (seen.has(user.id)) {
                        continue;
                    }
                    seen.add(user.id);
                    users.push(user);
                    added += 1;
                    if (!unlimited && users.length >= limit) {
                        break;
                    }
                }
                const pageCursor = page.cursor;
                if (!pageCursor || pageCursor === cursor || page.users.length === 0 || added === 0) {
                    nextCursor = undefined;
                    break;
                }
                if (maxPages && pagesFetched >= maxPages) {
                    nextCursor = pageCursor;
                    break;
                }
                cursor = pageCursor;
                nextCursor = pageCursor;
            }
            return { success: true, users, nextCursor };
        }
    }
    return TwitterClientFollowing;
}
