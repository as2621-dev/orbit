// Node-level, fully-offline test for the withFollowing mixin.
// Mocks globalThis.fetch with canned two-page Following GraphQL JSON and asserts:
//   (a) parses creator_handle/username out of user entries,
//   (b) follows next_cursor across BOTH pages (page-1-only would miss page-2 handles),
//   (c) the request carries the right /Following queryId + features + OAuth2Session headers.
// No live X call. No tokens in fixtures (dummy cookies only).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { TwitterClientBase } from '../lib/twitter-client-base.js';
import { withFollowing } from '../lib/twitter-client-following.js';

process.env.NODE_ENV = 'test'; // skip live queryId refresh / user-id lookup

const FollowingClient = withFollowing(TwitterClientBase);

/** Build a User timeline entry in the shape parseUsersFromInstructions expects. */
function userEntry(restId, screenName, name) {
    return {
        content: {
            itemContent: {
                user_results: {
                    result: {
                        __typename: 'User',
                        rest_id: restId,
                        legacy: { screen_name: screenName, name, followers_count: 42 },
                    },
                },
            },
        },
    };
}

/** Build a Bottom cursor entry in the shape extractCursorFromInstructions expects. */
function cursorEntry(value) {
    return { content: { cursorType: 'Bottom', value } };
}

function followingResponse(entries) {
    return {
        data: {
            user: {
                result: {
                    timeline: {
                        timeline: {
                            instructions: [{ type: 'TimelineAddEntries', entries }],
                        },
                    },
                },
            },
        },
    };
}

const PAGE_1 = followingResponse([
    userEntry('1001', 'alice', 'Alice'),
    userEntry('1002', 'bob', 'Bob'),
    cursorEntry('CURSOR_PAGE_2'),
]);

// Page 2: new handles + a terminal cursor that equals the incoming cursor -> walk stops.
const PAGE_2 = followingResponse([
    userEntry('1003', 'carol', 'Carol'),
    userEntry('1004', 'dave', 'Dave'),
    cursorEntry('CURSOR_PAGE_2'), // same value -> pagination terminates
]);

const PAGE_1_HANDLES = ['alice', 'bob'];

test('following walks the cursor across two pages and parses handles + request shape', async () => {
    const fetchCalls = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
        const body = JSON.parse(init.body);
        fetchCalls.push({ url, init, body });
        // Key the page on whether the request carried the page-2 cursor in variables.
        const variables = JSON.parse(new URL(url).searchParams.get('variables'));
        const payload = variables.cursor === 'CURSOR_PAGE_2' ? PAGE_2 : PAGE_1;
        return { ok: true, status: 200, json: async () => payload, text: async () => '' };
    };

    try {
        const client = new FollowingClient({
            cookies: { authToken: 'x', ct0: 'y' }, // dummy, never real tokens
            timeoutMs: 0,
        });
        const result = await client.following('987654321', { pageDelayMs: 0 });

        assert.equal(result.success, true, 'following should succeed');

        // (a) parse handles
        const handles = result.users.map((u) => u.username);
        assert.deepEqual(handles.sort(), ['alice', 'bob', 'carol', 'dave']);
        assert.equal(result.users[0].name, 'Alice');
        assert.equal(result.users[0].id, '1001');

        // (b) followed cursor across BOTH pages: union > page-1-only count
        assert.equal(fetchCalls.length, 2, 'should fetch two pages');
        assert.ok(
            handles.length > PAGE_1_HANDLES.length,
            'union across pages must exceed page-1-only count (no silent truncation)',
        );
        assert.ok(handles.includes('carol') && handles.includes('dave'), 'page-2 handles must be present');
        // second request must carry the advancing cursor
        const secondVars = JSON.parse(new URL(fetchCalls[1].url).searchParams.get('variables'));
        assert.equal(secondVars.cursor, 'CURSOR_PAGE_2', 'cursor must advance on page 2');

        // (c) request shape
        const { url, init, body } = fetchCalls[0];
        assert.ok(url.includes('/Following'), 'URL must hit the Following op');
        // queryId: one of the baked Following fallbacks
        assert.ok(
            ['BEkNpEt5pNETESoqMsTEGA', 'mWYeougg_ocJS2Vr1Vt28w'].some((qid) => url.includes(qid)),
            'URL must contain a Following queryId',
        );
        assert.ok(body.features && typeof body.features === 'object', 'POST body must carry features');
        assert.ok(typeof body.queryId === 'string' && body.queryId.length > 0, 'POST body must carry queryId');
        // Following features sanity
        assert.equal(body.features.responsive_web_graphql_timeline_navigation_enabled, true);
        // headers
        assert.equal(init.headers['x-csrf-token'], 'y', 'csrf must equal ct0');
        assert.ok(init.headers.authorization.startsWith('Bearer '), 'Bearer auth header present');
        assert.equal(init.headers['x-twitter-auth-type'], 'OAuth2Session');
    } finally {
        globalThis.fetch = originalFetch;
    }
});

test('following rejects a non-numeric target with an actionable error', async () => {
    const client = new FollowingClient({ cookies: { authToken: 'x', ct0: 'y' }, timeoutMs: 0 });
    const result = await client.following('alice');
    assert.equal(result.success, false);
    assert.match(result.error, /numeric userId/i);
});
