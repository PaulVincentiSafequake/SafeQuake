import { useEffect, useState, useCallback } from "react";
import {
  View,
  Text,
  StyleSheet,
  ScrollView,
  TouchableOpacity,
  Share,
  Platform,
  RefreshControl,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack, useRouter } from "expo-router";
import * as Notifications from "expo-notifications";
import {
  useAudioPlayer,
  useAudioPlayerStatus,
  setAudioModeAsync,
} from "expo-audio";

import {
  registerForPushNotifications,
  getDiagInfo,
  type DiagInfo,
} from "@/src/utils/push";
import { AppleWatchNote } from "@/src/components/AppleWatchNote";
import AsyncStorage from "@react-native-async-storage/async-storage";

// Local siren assets — used only to verify that the audio files are correctly
// bundled inside the native IPA/APK. `siren.caf` is the file APNs references
// for iOS Critical Alerts; if it doesn't play here, the push payload won't
// find it either.
const SIREN_CAF = require("../assets/audio/siren.caf");
const SIREN_MP3 = require("../assets/audio/siren.mp3");

interface PermStatus {
  status: string;
  granted: boolean;
  canAskAgain: boolean;
  ios?: {
    allowsAlert?: boolean | null;
    allowsSound?: boolean | null;
    allowsBadge?: boolean | null;
    allowsCriticalAlerts?: boolean | null;
    allowsAnnouncements?: boolean | null;
  } | null;
}

export default function DiagScreen() {
  const router = useRouter();
  const [info, setInfo] = useState<DiagInfo | null>(null);
  const [perm, setPerm] = useState<PermStatus | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // #252 (Batch 7 D): the Diagnostics screen was shipping developer
  // content — push tokens, device IDs, "HTTP 201", "bundled in the
  // IPA" — to every user. That is developer content on a consumer
  // screen. New default: show a human "Is this working?" summary and
  // hide all technical rows behind an explicit reveal.
  const [showTech, setShowTech] = useState(false);

  // Test-siren players. We keep two independent players so the user can
  // validate BOTH bundled audio assets (the .caf used by APNs Critical
  // Alerts and the .mp3 used by the in-app looping siren).
  const cafPlayer = useAudioPlayer(SIREN_CAF);
  const cafStatus = useAudioPlayerStatus(cafPlayer);
  const mp3Player = useAudioPlayer(SIREN_MP3);
  const mp3Status = useAudioPlayerStatus(mp3Player);

  const cafPlaying = !!cafStatus?.playing;
  const mp3Playing = !!mp3Status?.playing;

  const stopBothSirens = useCallback(() => {
    try {
      cafPlayer.pause();
      cafPlayer.seekTo(0);
    } catch {}
    try {
      mp3Player.pause();
      mp3Player.seekTo(0);
    } catch {}
  }, [cafPlayer, mp3Player]);

  const playCaf = useCallback(async () => {
    try {
      // Route audio through the loud ringer path even in silent mode so this
      // test faithfully mirrors what the user would hear on a real alert.
      await setAudioModeAsync({
        playsInSilentMode: true,
        shouldPlayInBackground: false,
        interruptionMode: "doNotMix",
      });
    } catch {}
    try {
      mp3Player.pause();
    } catch {}
    try {
      cafPlayer.loop = false;
      cafPlayer.volume = 1.0;
      cafPlayer.seekTo(0);
      cafPlayer.play();
      setMsg("Playing siren.caf — if you hear this, the .caf is bundled.");
    } catch (e) {
      setMsg(`siren.caf failed: ${(e as Error)?.message ?? "unknown"}`);
    }
  }, [cafPlayer, mp3Player]);

  const playMp3 = useCallback(async () => {
    try {
      await setAudioModeAsync({
        playsInSilentMode: true,
        shouldPlayInBackground: false,
        interruptionMode: "doNotMix",
      });
    } catch {}
    try {
      cafPlayer.pause();
    } catch {}
    try {
      mp3Player.loop = false;
      mp3Player.volume = 1.0;
      mp3Player.seekTo(0);
      mp3Player.play();
      setMsg("Playing siren.mp3 — in-app siren asset.");
    } catch (e) {
      setMsg(`siren.mp3 failed: ${(e as Error)?.message ?? "unknown"}`);
    }
  }, [cafPlayer, mp3Player]);

  const load = useCallback(async () => {
    const [i, p] = await Promise.all([
      getDiagInfo(),
      Notifications.getPermissionsAsync(),
    ]);
    setInfo(i);
    setPerm({
      status: p.status,
      granted: p.granted,
      canAskAgain: p.canAskAgain,
      ios: (p as any).ios ?? null,
    });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Ensure sirens don't keep looping if the user navigates away.
  useEffect(() => {
    return () => {
      stopBothSirens();
    };
  }, [stopBothSirens]);

  // QA affordance (Paul, 2026-08-18): the post-update Apple Watch notice
  // only reappears after a version change or 14 days, so re-testing it
  // previously meant reinstalling the app. This clears the three keys that
  // hold its state, so the notice reappears on the next Home visit.
  const onResetWatchReminder = useCallback(async () => {
    await AsyncStorage.multiRemove([
      "quakeguard_watch_confirmed_at",
      "quakeguard_watch_confirmed_version",
      "quakeangel_no_apple_watch",
    ]);
    setMsg("Apple Watch reminder reset — go Back to Home to see it.");
  }, []);

  const onReRegister = useCallback(async () => {
    setBusy("registering");
    setMsg(null);
    try {
      await registerForPushNotifications();
      await load();
      setMsg("Re-registered with backend. Check last-registrations log.");
    } catch (e) {
      setMsg(`Failed: ${(e as Error)?.message ?? "unknown"}`);
    } finally {
      setBusy(null);
    }
  }, [load]);

  const onCopyUserId = useCallback(async () => {
    if (!info?.user_id) return;
    try {
      await Share.share({ message: info.user_id });
    } catch {}
  }, [info?.user_id]);

  const onCopyToken = useCallback(async () => {
    if (!info?.device_token) return;
    try {
      await Share.share({ message: info.device_token });
    } catch {}
  }, [info?.device_token]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }, [load]);

  return (
    <SafeAreaView style={styles.safe} edges={["top", "bottom"]}>
      <Stack.Screen
        options={{
          title: "Diagnostics",
          headerStyle: { backgroundColor: "#0e1116" },
          headerTintColor: "#fff",
        }}
      />
      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.scrollBody}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={onRefresh}
            tintColor="#c21818"
          />
        }
      >
        <View style={styles.header}>
          <Text style={styles.h1}>Is this working?</Text>
          <Text style={styles.subtitle}>
            A plain-language health check for the parts that keep you
            alerted. Pull down to refresh.
          </Text>
        </View>

        {/* #252 (Batch 7 D): the top of the screen is now a human
            "yes/no" health summary. Every line reads as an answer the
            person could give out loud on the phone — no tokens, no
            HTTP codes, no "bundled in the IPA". */}
        <Section title="Health check">
          {(() => {
            const critical = perm?.ios?.allowsCriticalAlerts ?? null;
            const alertsOn = !!(perm?.granted && perm?.ios?.allowsAlert !== false);
            const soundOn = !!perm?.ios?.allowsSound;
            const registered = !!info?.token_length && info.token_length > 0;
            const lastStatus = String(info?.last_register_status ?? "");
            // Ok if the last registration reached the server — the
            // backend confirms with a 2xx (usually 201). We check the
            // FIRST digit rather than the exact code so a switch from
            // 201 to 200 doesn't silently break the summary.
            const serverOk = /^2\d\d/.test(lastStatus);
            const overallReady = alertsOn && registered && serverOk;
            return (
              <>
                <HealthRow
                  ok={overallReady}
                  yes="Yes — this app is ready to alert you"
                  no="Not quite — something below needs attention"
                />
                <HealthRow
                  ok={alertsOn}
                  yes="Alerts are switched on for this app"
                  no="Alerts are switched off. Open Settings › Notifications › Quake Angel and turn them on."
                />
                {Platform.OS === "ios" ? (
                  <HealthRow
                    ok={!!critical}
                    yes="This app can override Do Not Disturb (Critical Alerts)"
                    no="Critical Alerts are off — a real alarm will still ring, but a call or Do Not Disturb can mute it. Open Settings › Notifications › Quake Angel and turn on Critical Alerts."
                  />
                ) : null}
                <HealthRow
                  ok={soundOn}
                  yes="This app can make sound"
                  no="Sound for this app is off — the siren can't play. Turn it on in Settings."
                />
                <HealthRow
                  ok={registered}
                  yes="Your phone is signed up to receive alerts"
                  no="Your phone hasn't finished signing up. Tap Try again below."
                />
                <HealthRow
                  ok={serverOk}
                  yes="The server has confirmed it can reach your phone"
                  no="The server hasn't confirmed reach yet. Pull down to refresh, or tap Try again below."
                />
              </>
            );
          })()}
        </Section>

        <Section title="This build">
          {/* #251 (Batch 7 R4): the leading version number here reads
              from the SAME `info.app_version` as the version row, so
              the two are guaranteed to agree. The DESCRIPTION stays a
              hard-coded string so a build shipping without the fix is
              caught by the mismatch. */}
          <Row label="Version" value={info?.app_version ?? "—"} />
          <Row
            label="What's fixed in it"
            value={
              (info?.app_version ?? "?") +
              " — #208 R4 primary alert routes to check-in, #245 type-to-confirm on trigger, #199 clear-on-stand-down, #247 saved place no longer points at wrong city, #249 saved places on the map, #211 recency-ramp map key, #243 zoom in to epicentre, #250 not-responding wording, #252 human-first diagnostics, #244 honest test-button wording."
            }
          />
        </Section>

        <Section title="Siren">
          <Text style={styles.help}>
            Tap Trigger Test Alert on the home screen: the siren must start
            within about a second of the red screen appearing, and stop the
            instant you tap I&apos;M SAFE or choose an injury option.
            {"\n\n"}
            <Text style={styles.helpBold}>What this test proves:</Text> the
            red alert screen, the siren audio, and the check-in buttons all
            work on THIS phone right now.
            {"\n"}
            <Text style={styles.helpBold}>What it does NOT prove:</Text> that
            a real push notification would reach your phone from the server.
            That path involves Apple/Google, and we can only confirm it end
            to end from the dashboard&apos;s Trigger Earthquake Alert button
            or by watching a real notification arrive.
          </Text>
        </Section>

        <Section title="Aftershock rehearsal">
          <Text style={styles.help}>
            Shows what happens when a second earthquake alert arrives while you
            are part-way through answering the first. Tap the button below, then
            start answering — choose I NEED HELP and pick an injury level, but
            do not send it. After 12 seconds a second alert arrives.
            {"\n\n"}
            What you should see: the screen does NOT restart. An amber notice
            appears at the top saying another alert arrived, and your
            part-finished answer is exactly where you left it.
            {"\n\n"}
            This uses the same internal path a real second alert uses. The
            one thing it cannot test is Apple&apos;s delivery of the second
            notification — that's a network round-trip, and this rehearsal
            runs entirely on your phone.
          </Text>
        </Section>

        <TouchableOpacity
          style={styles.btnGhost}
          onPress={() => router.push("/alert?siren=1&test=1&rehearse=aftershock" as any)}
          testID="diag-aftershock-rehearsal"
        >
          <Text style={styles.btnGhostText}>Rehearse an aftershock mid-answer</Text>
        </TouchableOpacity>

        <Section title="Test siren on this phone">
          <Text style={styles.help}>
            Plays the siren sound locally so you can check it&apos;s not
            silenced by your phone&apos;s ringer or volume settings. This
            plays entirely on your device — no notification is sent.
          </Text>
        </Section>

        <View style={styles.testRow}>
          <TouchableOpacity
            style={[styles.testBtn, styles.testBtnCaf, cafPlaying && styles.testBtnActive]}
            onPress={cafPlaying ? stopBothSirens : playCaf}
            activeOpacity={0.85}
          >
            <Text style={styles.testBtnText}>
              {cafPlaying ? "Stop siren" : "Play siren (alert sound)"}
            </Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.testBtn, styles.testBtnMp3, mp3Playing && styles.testBtnActive]}
            onPress={mp3Playing ? stopBothSirens : playMp3}
            activeOpacity={0.85}
          >
            <Text style={styles.testBtnText}>
              {mp3Playing ? "Stop siren" : "Play siren (in-app)"}
            </Text>
          </TouchableOpacity>
        </View>

        {msg ? (
          <View style={styles.msg}>
            <Text style={styles.msgText}>{msg}</Text>
          </View>
        ) : null}

        <TouchableOpacity
          style={[styles.btn, busy === "registering" && styles.btnDisabled]}
          onPress={onReRegister}
          disabled={busy !== null}
        >
          <Text style={styles.btnText}>
            {busy === "registering" ? "Trying…" : "Try again"}
          </Text>
        </TouchableOpacity>

        {Platform.OS !== "android" ? (
          <View style={styles.section}>
            <Text style={styles.sectionTitle}>Apple Watch behavior</Text>
            <AppleWatchNote variant="compact" />
          </View>
        ) : null}

        <TouchableOpacity
          style={styles.btnGhost}
          onPress={onResetWatchReminder}
        >
          <Text style={styles.btnGhostText}>Reset Apple Watch reminder</Text>
        </TouchableOpacity>

        {/* #252 (Batch 7 D): all technical rows live behind an explicit
            reveal. If a support caller needs a rescue code or token
            fingerprint they can find it in one tap; nothing else needs
            to see it. */}
        <TouchableOpacity
          style={styles.techToggle}
          onPress={() => setShowTech((v) => !v)}
          accessibilityRole="button"
          accessibilityState={{ expanded: showTech }}
          testID="diag-tech-toggle"
        >
          <Text style={styles.techToggleText}>
            {showTech ? "Hide technical details" : "Show technical details (for support)"}
          </Text>
        </TouchableOpacity>

        {showTech ? (
          <>
            <Section title="For support">
              <Row label="Rescue code" value={
                info?.user_id ? String(info.user_id).slice(-5).toUpperCase() : "—"
              } mono />
              <Row label="Device identifier" value={info?.user_id ?? "—"} mono onPress={onCopyUserId} />
              <Row label="Phone type" value={info?.platform?.toUpperCase() ?? "—"} />
              <Row label="Build number" value={info?.build_number ?? "—"} />
            </Section>

            <Section title="Push token">
              <Row
                label="Fingerprint"
                value={info?.token_fingerprint ?? "(no token)"}
                mono
                onPress={info?.device_token ? onCopyToken : undefined}
              />
              <Row label="Length" value={String(info?.token_length ?? 0)} />
              <Row
                label="Expected"
                value={
                  info?.platform === "ios"
                    ? "~64 hex chars (APNs)"
                    : "~150+ chars (FCM)"
                }
                hint
              />
            </Section>

            <Section title="Permissions">
              <Row
                label="Status"
                value={perm?.status ?? "—"}
                valueColor={perm?.granted ? "#1F8A3A" : "#c21818"}
              />
              <Row label="Can ask again" value={perm?.canAskAgain ? "yes" : "no"} />
              {perm?.ios ? (
                <>
                  <Row label="Alert" value={perm.ios.allowsAlert ? "yes" : "no"} />
                  <Row label="Sound" value={perm.ios.allowsSound ? "yes" : "no"} />
                  <Row
                    label="Critical Alerts"
                    value={perm.ios.allowsCriticalAlerts ? "yes" : "no"}
                    valueColor={perm.ios.allowsCriticalAlerts ? "#1F8A3A" : "#c21818"}
                  />
                </>
              ) : null}
            </Section>

            <Section title="Registration with server">
              <Row label="Server" value={info?.backend_url ?? "—"} mono />
              <Row label="Last attempt" value={info?.last_registered_at ?? "never"} />
              <Row
                label="Last result"
                value={(() => {
                  const s = String(info?.last_register_status ?? "");
                  if (!s || s === "—") return "—";
                  if (/^2\d\d/.test(s)) return "OK — server confirmed";
                  if (/^4\d\d/.test(s)) return "Not accepted — try Re-register above";
                  if (/^5\d\d/.test(s)) return "Server had trouble — will retry";
                  return s;
                })()}
              />
              <Row
                label="Siren asset"
                value={cafStatus?.isLoaded ? "loaded" : "not loaded"}
                valueColor={cafStatus?.isLoaded ? "#1F8A3A" : "#c21818"}
              />
              <Row
                label="In-app siren"
                value={mp3Status?.isLoaded ? "loaded" : "not loaded"}
                valueColor={mp3Status?.isLoaded ? "#1F8A3A" : "#c21818"}
              />
            </Section>
          </>
        ) : null}

        <TouchableOpacity
          style={styles.btnGhost}
          onPress={() => router.back()}
        >
          <Text style={styles.btnGhostText}>Back</Text>
        </TouchableOpacity>

        <Text style={styles.footer}>
          Pull down to refresh. Long-press a code to share it with support.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      <View style={styles.card}>{children}</View>
    </View>
  );
}

/** #252 (Batch 7 D): a single yes/no line in the "Is this working?"
 *  panel. Green tick + short sentence when things are OK; red circle
 *  + plain-language "what to do next" when they aren't. Never shows
 *  a status code, token, or URL. */
function HealthRow({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return (
    <View style={styles.healthRow}>
      <Text style={[styles.healthGlyph, ok ? styles.healthGlyphOk : styles.healthGlyphBad]}>
        {ok ? "✓" : "✕"}
      </Text>
      <Text style={styles.healthText}>{ok ? yes : no}</Text>
    </View>
  );
}

function Row({
  label,
  value,
  mono,
  hint,
  valueColor,
  onPress,
}: {
  label: string;
  value: string;
  mono?: boolean;
  hint?: boolean;
  valueColor?: string;
  onPress?: () => void;
}) {
  const inner = (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text
        style={[
          styles.rowValue,
          mono && styles.mono,
          hint && styles.hint,
          valueColor ? { color: valueColor } : null,
        ]}
        numberOfLines={2}
        selectable
      >
        {value}
      </Text>
    </View>
  );
  if (onPress) {
    return (
      <TouchableOpacity onLongPress={onPress} activeOpacity={0.7}>
        {inner}
      </TouchableOpacity>
    );
  }
  return inner;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "#0e1116" },
  scroll: { flex: 1, backgroundColor: "#0e1116" },
  scrollBody: { padding: 16, paddingBottom: 40 },
  header: { marginBottom: 16 },
  help: { color: "#8a94a6", fontSize: 13, lineHeight: 19 },
  helpBold: { color: "#e6e8ec", fontWeight: "700" },
  healthRow: {
    flexDirection: "row",
    gap: 12,
    alignItems: "flex-start",
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#242a34",
  },
  healthGlyph: {
    fontSize: 18,
    fontWeight: "800",
    lineHeight: 22,
    width: 22,
    textAlign: "center",
  },
  healthGlyphOk: { color: "#1F8A3A" },
  healthGlyphBad: { color: "#c21818" },
  healthText: { color: "#e6e8ec", fontSize: 14, flex: 1, lineHeight: 20 },
  techToggle: {
    borderRadius: 10,
    paddingVertical: 12,
    alignItems: "center",
    marginTop: 20,
    marginBottom: 4,
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: "#3a4051",
  },
  techToggleText: {
    color: "#8a94a6", fontSize: 13, fontWeight: "600",
  },
  h1: { color: "#fff", fontSize: 22, fontWeight: "700" },
  subtitle: { color: "#8a94a6", fontSize: 13, marginTop: 4 },
  section: { marginBottom: 16 },
  sectionTitle: {
    color: "#8a94a6",
    fontSize: 12,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.5,
    marginBottom: 8,
    marginLeft: 4,
  },
  card: {
    backgroundColor: "#1b1f27",
    borderRadius: 12,
    paddingHorizontal: 14,
    borderWidth: 1,
    borderColor: "#242a34",
  },
  row: {
    flexDirection: "row",
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#242a34",
    alignItems: "flex-start",
  },
  rowLabel: {
    color: "#8a94a6",
    fontSize: 13,
    width: 110,
    paddingTop: 1,
  },
  rowValue: {
    color: "#e6e8ec",
    fontSize: 13,
    flex: 1,
    textAlign: "right",
  },
  mono: {
    fontFamily: Platform.select({ ios: "Menlo", android: "monospace", default: "monospace" }),
    fontSize: 12,
  },
  hint: { color: "#666e7d", fontStyle: "italic" },
  msg: {
    backgroundColor: "#1e2a20",
    borderColor: "#2a4a30",
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
    marginBottom: 12,
  },
  msgText: { color: "#a5d6a7", fontSize: 13 },
  testRow: {
    flexDirection: "row",
    gap: 10,
    marginBottom: 12,
  },
  testBtn: {
    flex: 1,
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center",
    borderWidth: 1,
  },
  testBtnCaf: {
    backgroundColor: "#3a1a1a",
    borderColor: "#c21818",
  },
  testBtnMp3: {
    backgroundColor: "#1a2a3a",
    borderColor: "#2f6feb",
  },
  testBtnActive: {
    backgroundColor: "#c21818",
    borderColor: "#ff5555",
  },
  testBtnText: { color: "#fff", fontSize: 14, fontWeight: "700" },
  btn: {
    backgroundColor: "#c21818",
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center",
    marginTop: 4,
  },
  btnDisabled: { opacity: 0.5 },
  btnText: { color: "#fff", fontSize: 15, fontWeight: "700" },
  btnGhost: {
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center",
    marginTop: 10,
    borderWidth: 1,
    borderColor: "#2a303b",
  },
  btnGhostText: { color: "#8a94a6", fontSize: 14, fontWeight: "600" },
  footer: {
    color: "#5b6472",
    fontSize: 11,
    textAlign: "center",
    marginTop: 20,
  },
});
