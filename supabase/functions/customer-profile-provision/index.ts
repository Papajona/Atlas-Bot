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

    // Do not silently create a second identity for an email already present on
    // one of the 18 legacy profiles whose old Tron-based Auth reference is not a
    // Supabase UUID. Such accounts require manual identity proof and mapping.
    const { data: emailMatches, error: emailLookupError } = await profiles
      .select("id,auth_user_id")
      .ilike("email", email)
      .limit(2);

    if (emailLookupError) {
      return response({ error: "Existing customer identity check failed" }, 503);
    }
    if ((emailMatches ?? []).some((profile: { auth_user_id: string }) => profile.auth_user_id !== userId)) {
      return response({ error: "This email is already associated with an existing customer profile; manual identity review is required" }, 409);
    }

    const metadata = (user.user_metadata ?? {}) as Record<string, unknown>;
    const displayName = String(metadata.full_name ?? metadata.name ?? "")
      .trim()
      .slice(0, 160);
    const now = new Date().toISOString();

    const { data: created, error: insertError } = await profiles
      .insert({
        auth_user_id: userId,
        email: email.slice(0, 320),
        display_name: displayName,
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
