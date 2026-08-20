/**
 * EntitlementBanner — subscription-state banner shown on home (Phase B).
 *
 * Design contract (Paul, 2026-08-06):
 *   1. NEVER blocks app content. Non-modal, always dismissable, sits
 *      inline in the home ScrollView above the safety-protocol cards.
 *   2. Copy comes 100% from the backend `/api/entitlement` response.
 *      This module never invents strings — the mobile layer can't
 *      subtly shift wording that legal / App Store review sees. The
 *      only thing this component decides is colour + iconography per
 *      `banner.kind`.
 *   3. Every banner variant ends with "Critical alerts still work."
 *      That invariant is enforced backend-side in
 *      `/app/backend/entitlements.py`.
 *   4. CTA action is a string opcode ("manage_subscription" |
 *      "resubscribe"). This screen maps it to the platform-native flow
 *      (Linking to iOS's `itms-apps://apps.apple.com/account/subscriptions`
 *      which triggers `showManageSubscriptions`-equivalent from a web
 *      context). Actual native StoreKit purchase flow lands in Phase C.
 *   5. If a user dismisses the banner, it stays dismissed for that
 *      state+session. Reappears on next launch (persisted per-state
 *      dismissal so an "active" -> "grace" transition surfaces a new
 *      banner even if the previous one was dismissed).
 */
import { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, TouchableOpacity, Linking, Platform,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL = process.env.EXPO_PUBLIC_BACKEND_URL ?? "";

type BannerKind = "info" | "warn" | "urgent";
type CtaAction = "manage_subscription" | "resubscribe";

type Banner = {
  kind: BannerKind;
  title: string;
  body: string;
  cta_label: string;
  cta_action: CtaAction;
  dismissable: boolean;
};

type EntitlementResponse = {
  state: string;
  plan: string | null;
  banner: Banner | null;
  critical_alerts_active: boolean;
};

// Per-state dismissal key. Dismissing "grace" doesn't hide the later
// "lapsed" banner — user gets a fresh chance to see the state change.
const DISMISS_KEY = (state: string) => `entitlement.banner.dismissed.${state}`;

// Colour palette per kind. Tuned to sit on the app's dark background
// without shouting — this is meant to be noticeable, not alarming.
// "urgent" is reserved for future critical-billing scenarios (e.g.
// entitlement.state falls into "lapsed" with an active event nearby).
const KIND_STYLE: Record<BannerKind, {
  border: string; bg: string; icon: string; iconColor: string; ctaBg: string; ctaText: string;
}> = {
  info:   { border: "#3D4454", bg: "#151E2F", icon: "information-circle", iconColor: "#5DB1FF", ctaBg: "#0F2540", ctaText: "#5DB1FF" },
  warn:   { border: "#8A6B0F", bg: "#2A2216", icon: "alert-circle",       iconColor: "#F4C842", ctaBg: "#3A2A0F", ctaText: "#F4C842" },
  urgent: { border: "#8A0F0F", bg: "#2A1616", icon: "warning",            iconColor: "#E64545", ctaBg: "#3A0F0F", ctaText: "#FFB4B4" },
};

export default function EntitlementBanner() {
  const [banner, setBanner] = useState<Banner | null>(null);
  const [state, setState] = useState<string>("never_subscribed");
  const [dismissed, setDismissed] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const did = await getDeviceId();
        const r = await fetch(`${BACKEND_URL}/api/entitlement?device_id=${encodeURIComponent(did)}`);
        if (!r.ok) return;
        const data: EntitlementResponse = await r.json();
        if (cancelled) return;

        // SAFETY INVARIANT check: if the backend ever violates the
        // "critical alerts always on" promise, the banner refuses to
        // render at all — better to show nothing than to show a
        // "critical alerts disabled" state that the whole product
        // depends on being impossible. This is a belt-and-braces
        // defence; the backend is authoritative but the client
        // audits it.
        if (data.critical_alerts_active === false) {
          if (__DEV__) {
            console.warn("[EntitlementBanner] Backend returned critical_alerts_active=false — refusing to render. This is a bug in the entitlement state machine.");
          }
          setBanner(null);
          return;
        }

        setBanner(data.banner);
        setState(data.state);

        // Restore per-state dismissal.
        if (data.banner) {
          const stored = await AsyncStorage.getItem(DISMISS_KEY(data.state));
          if (stored === "true") setDismissed(true);
        }
      } catch {
        // Offline / backend down — no banner. That is the correct
        // behaviour: we don't want a stale banner nagging a user
        // who's disconnected. The next successful fetch will refresh.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (!banner || dismissed) return null;

  const s = KIND_STYLE[banner.kind] ?? KIND_STYLE.info;

  const handleCta = () => {
    // Both actions route the user to the OS-native subscription-
    // management surface. Apple explicitly requires this route (not a
    // web checkout) for App Store apps.
    // iOS: itms-apps:// deep-links into Settings > Subscriptions.
    // Android: market:// opens the Play Store subscriptions page.
    const url = Platform.select({
      ios: "itms-apps://apps.apple.com/account/subscriptions",
      android: "https://play.google.com/store/account/subscriptions",
      default: "https://apps.apple.com/account/subscriptions",
    });
    Linking.openURL(url as string).catch(() => { /* ignore — user can retry */ });
  };

  const handleDismiss = async () => {
    setDismissed(true);
    try {
      await AsyncStorage.setItem(DISMISS_KEY(state), "true");
    } catch { /* AsyncStorage failure isn't worth alarming the user */ }
  };

  return (
    <View
      style={[styles.card, { borderColor: s.border, backgroundColor: s.bg }]}
      accessibilityRole="alert"
      accessibilityLabel={`${banner.title}. ${banner.body}`}
    >
      <View style={styles.headerRow}>
        <Ionicons name={s.icon as any} size={20} color={s.iconColor} />
        <Text style={styles.title}>{banner.title}</Text>
        {banner.dismissable && (
          <TouchableOpacity
            onPress={handleDismiss}
            hitSlop={{top:10,bottom:10,left:10,right:10}}
            accessibilityRole="button"
            accessibilityLabel="Dismiss banner"
            style={styles.dismissBtn}
          >
            <Ionicons name="close" size={18} color="#8FA0BC" />
          </TouchableOpacity>
        )}
      </View>

      <Text style={styles.body}>{banner.body}</Text>

      <TouchableOpacity
        onPress={handleCta}
        style={[styles.cta, { backgroundColor: s.ctaBg, borderColor: s.border }]}
        accessibilityRole="button"
        accessibilityLabel={banner.cta_label}
        testID="entitlement-banner-cta"
      >
        <Text style={[styles.ctaText, { color: s.ctaText }]}>{banner.cta_label}</Text>
        <Ionicons name="chevron-forward" size={16} color={s.ctaText} />
      </TouchableOpacity>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    marginHorizontal: 16, marginTop: 12, marginBottom: 4,
    padding: 14, borderRadius: 12, borderWidth: 1,
    gap: 8,
  },
  headerRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
  },
  title: {
    flex: 1, color: "#E7EDF5", fontSize: 15, fontWeight: "700",
  },
  dismissBtn: { padding: 2 },
  body: { color: "#B3BCCC", fontSize: 14, lineHeight: 19 },
  cta: {
    flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 10, paddingHorizontal: 14,
    borderRadius: 8, borderWidth: 1, marginTop: 4,
    minHeight: 44,   // iOS min touch target
  },
  ctaText: { fontSize: 14, fontWeight: "700" },
});
