import { withSupabase } from "npm:@supabase/server@^1";

const response = (body: Record<string, unknown>, status = 200) =>
  Response.json(body, {
    status,
    headers: { "Cache-Control": "no-store" },
  });

export default {
  fetch: withSupabase({ auth: "user" }, async (req, ctx) => {
    if (req.method !== "POST") {
      return response({ error: "Method not allowed" }, 405);
    }

    // @supabase/server exposes the resolved identity separately from the
    // full JWT. Use userClaims.id for identity and jwtClaims for sub/aal.
    // The full signed JWT must represent a completed second-factor session.
    const userClaims = ctx.userClaims ?? {};
    const jwtClaims = ctx.jwtClaims ?? {};
    const userId = String(userClaims.id ?? "").trim();
    const jwtSubject = String(jwtClaims.sub ?? "").trim();
    if (!userId || userId !== jwtSubject || jwtClaims.aal !== "aal2") {
      return response({ error: "A verified AAL2 session is required" }, 403);
    }

    // Re-resolve the identity and factors from Supabase Auth. Never accept a
    // caller-supplied user ID, email, customer ID, status, or display name.
    const { data: authData, error: authError } = await ctx.supabase.auth.getUser();
    const user = authData?.user;
    if (authError || !user || user.id !== userId) {
      return response({ error: "The authenticated Supabase user could not be verified" }, 401);
    }

    const email = String(user.email ?? "").trim().toLowerCase();
    if (!email || !user.email_confirmed_at) {
      return response({ error: "A confirmed email address is required" }, 403);
    }

    const { data: factorData, error: factorError } = await ctx.supabase.auth.mfa.listFactors();
    if (factorError) {
      return response({ error: "Unable to verify the account authenticator" }, 503);
    }
    const hasVerifiedTotp = (factorData?.totp ?? []).some((factor) => factor.status === "verified");
    if (!hasVerifiedTotp) {
      return response({ error: "A verified TOTP authenticator is required" }, 403);
    }

    const profiles = ctx.supabaseAdmin.from("customer_profiles");
    const { data: existing, error: lookupError } = await profiles
      .select("id,status")
      .eq("auth_user_id", userId)
      .maybeSingle();

    if (lookupError) {
      return response({ error: "Customer profile lookup failed" }, 503);
    }
    if (existing) {
      // Idempotent read-only replay. In particular, never rewrite an existing
      // legacy customer record or change its email/status during provisioning.
      return response({
        status: "ALREADY_PROVISIONED",
        customer_id: existing.id,
        account_status: existing.status,
      });
    }

    // Existing legacy profiles have non-UUID Auth references and blank encrypted
    // email fields. No safe comparison can establish that a new Auth user is a
    // different person. Fail closed for new identities until support completes
    // the legacy identity mapping; existing UUID-linked users remain unaffected.
    const pageSize = 500;
    for (let offset = 0; ; offset += pageSize) {
      const { data: profilePage, error: legacyLookupError } = await ctx.supabaseAdmin
        .from("customer_profiles")
        .select("auth_user_id")
        .range(offset, offset + pageSize - 1);
      if (legacyLookupError || !Array.isArray(profilePage)) {
        return response({ error: "Legacy customer identity verification is unavailable; manual review is required" }, 503);
      }
      const hasUnmappedLegacyIdentity = profilePage.some((profile: { auth_user_id?: unknown }) => {
        const reference = String(profile.auth_user_id ?? "").trim();
        return !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(reference);
      });
      if (hasUnmappedLegacyIdentity) {
        return response({
          error: "New customer provisioning is paused while legacy customer identities are manually reviewed. Contact support.",
        }, 409);
      }
      if (profilePage.length < pageSize) break;
    }

    // Atlas stores customer_profiles.email and display_name with application-layer
    // Fernet encryption. This Edge Function intentionally has no encryption key,
    // so it must leave both default-empty fields untouched. The paper-staging API
    // reads confirmed email/display name from Supabase Auth instead.
    //
    // Legacy profiles currently have blank email fields and non-UUID legacy Auth
    // references, so there is no safe email-based auto-link. Never rewrite or
    // infer a legacy identity from a caller-supplied address.
    const now = new Date().toISOString();

    const { data: created, error: insertError } = await profiles
      .insert({
        // email/display_name deliberately omitted: Atlas EncryptedText fields
        // default to the empty string and must not receive plaintext here.
        auth_user_id: userId,
        status: "ACTIVE",
        created_at: now,
        updated_at: now,
      })
      .select("id,status")
      .single();

    if (insertError) {
      // Another request may have won the unique-key race. Read back its row
      // without changing it; all other insert failures remain closed.
      if (insertError.code === "23505") {
        const { data: raced, error: raceLookupError } = await profiles
          .select("id,status")
          .eq("auth_user_id", userId)
          .maybeSingle();
        if (!raceLookupError && raced) {
          return response({
            status: "ALREADY_PROVISIONED",
            customer_id: raced.id,
            account_status: raced.status,
          });
        }
      }
      return response({ error: "Customer profile could not be provisioned" }, 503);
    }

    return response({
      status: "PROVISIONED",
      customer_id: created.id,
      account_status: created.status,
    }, 201);
  }),
};
