import { NextRequest, NextResponse } from 'next/server';
import { getBackendConfig } from '../_lib/backend-config';

/**
 * Where to send the user after login: a relative path ("/x", not "//x" or "/\x") or an absolute
 * URL on this console's own origin. Anything else (another site) falls back to "/".
 */
function safeRedirectUri(req: NextRequest, value: string | null): string {
    if (!value) return '/';
    if (value.startsWith('/')) {
        return value.startsWith('//') || value.startsWith('/\\') ? '/' : value;
    }
    try {
        const url = new URL(value);
        return url.origin === req.nextUrl.origin ? url.toString() : '/';
    } catch {
        return '/';
    }
}

export async function GET(req: NextRequest) {
    const redirectUri = safeRedirectUri(req, req.nextUrl.searchParams.get('redirect_uri'));

    const backendConfig = getBackendConfig();
    if (!backendConfig.iamUrl || !backendConfig.loginProviderId) {
        return NextResponse.json(
            { error: 'Login is not configured: IAM_URL and LOGIN_PROVIDER_ID must be set' },
            { status: 500 },
        );
    }
    const iamUrl = `${backendConfig.iamUrl}${"/auth/start_authentication_transaction"}`;

    const url = `${iamUrl}?id=${encodeURIComponent(backendConfig.loginProviderId)}&redirect_uri=${encodeURIComponent(redirectUri)}`;

    let data: { redirectUrl?: string };
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { accept: 'application/json' },
            body: ''
        });
        data = await res.json();
    } catch {
        return NextResponse.json({ error: 'IAM did not answer the login request' }, { status: 502 });
    }

    if (!data?.redirectUrl) {
        return NextResponse.json({ error: 'Failed to initiate auth' }, { status: 500 });
    }

    return NextResponse.redirect(data.redirectUrl);
}
