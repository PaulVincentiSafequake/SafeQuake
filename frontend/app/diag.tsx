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
          <Text style={styles.h1}>Push Diagnostics</Text>
          <Text style={styles.subtitle}>
            Verify this device&apos;s identity and push registration state.
          </Text>
        </View>

        <Section title="Device identity">
          <Row label="user_id" value={info?.user_id ?? "—"} mono onPress={onCopyUserId} />
          {/* Rescue code — the same 5-char tail shown prominently on the
              main screen and on the persistent lock-screen card fired
              after a trapped submission. Displayed here so support can
              read it back to a caller over the phone. */}
          <Row
            label="rescue code"
            value={
              info?.user_id
                ? String(info.user_id).slice(-5).toUpperCase()
                : "—"
            }
            mono
          />
          <Row label="platform" value={info?.platform?.toUpperCase() ?? "—"} />
          <Row label="app version" value={info?.app_version ?? "—"} />
          <Row label="build number" value={info?.build_number ?? "—"} />
          {/* Build marker (#169 aftermath). Deliberately a HARD-CODED row
              rather than a version comparison: this row only exists in the
              bundle that contains the fix, so its presence can't be wrong
              and its ABSENCE is itself the answer. Version lookups can
              disagree with the binary; a code marker can't. */}
          <Row label="fixes in this build" value="1.0.29 — #208 recheck routing hardened (never falls through to stats), cold-start action buttons honoured; carries prior 1.0.28 fixes" />
        </Section>

        <Section title="Siren">
          <Text style={styles.help}>
            Tap Trigger Test Alert on the home screen: the siren must start
            within about a second of the red screen appearing, and stop the
            instant you tap I&apos;M SAFE or choose an injury option.
            {"\n\n"}
            If the row above is missing, this build predates the #169 fix and
            the test button will be silent — the fix is app-side, so no
            backend publish can change that.
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
            part-finished answer is exactly where you left it. If you had
            already sent a report, the notice says so and offers an Update
            button instead — it never resets you on its own.
            {"\n\n"}
            This uses the same internal path a real second alert uses. The one
            thing it cannot test is Apple&apos;s delivery of the second
            notification.
          </Text>
        </Section>

        <TouchableOpacity
          style={styles.btnGhost}
          onPress={() => router.push("/alert?siren=1&test=1&rehearse=aftershock" as any)}
          testID="diag-aftershock-rehearsal"
        >
          <Text style={styles.btnGhostText}>Rehearse an aftershock mid-answer</Text>
        </TouchableOpacity>

        <Section title="Push token">
          <Row
            label="fingerprint"
            value={info?.token_fingerprint ?? "(no token)"}
            mono
            onPress={info?.device_token ? onCopyToken : undefined}
          />
          <Row label="length" value={String(info?.token_length ?? 0)} />
          <Row
            label="expected"
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
            label="status"
            value={perm?.status ?? "—"}
            valueColor={perm?.granted ? "#1F8A3A" : "#c21818"}
          />
          <Row label="canAskAgain" value={perm?.canAskAgain ? "yes" : "no"} />
          {perm?.ios ? (
            <>
              <Row label="alert" value={perm.ios.allowsAlert ? "yes" : "no"} />
              <Row label="sound" value={perm.ios.allowsSound ? "yes" : "no"} />
              <Row
                label="critical alerts"
                value={perm.ios.allowsCriticalAlerts ? "yes" : "no"}
                valueColor={perm.ios.allowsCriticalAlerts ? "#1F8A3A" : "#c21818"}
              />
            </>
          ) : null}
        </Section>

        <Section title="Registration">
          <Row label="backend" value={info?.backend_url ?? "—"} mono />
          <Row label="last at" value={info?.last_registered_at ?? "never"} />
          <Row label="last status" value={info?.last_register_status ?? "—"} />
        </Section>

        {Platform.OS !== "android" ? (
          <View style={styles.section}>
            <Text style={styles.sectionTitle}>Apple Watch behavior</Text>
            <AppleWatchNote variant="compact" />
          </View>
        ) : null}

        <Section title="Test siren (local playback)">
          <Row
            label="siren.caf"
            value={cafStatus?.isLoaded ? (cafPlaying ? "playing" : "loaded") : "not loaded"}
            valueColor={cafStatus?.isLoaded ? "#1F8A3A" : "#c21818"}
          />
          <Row
            label="siren.mp3"
            value={mp3Status?.isLoaded ? (mp3Playing ? "playing" : "loaded") : "not loaded"}
            valueColor={mp3Status?.isLoaded ? "#1F8A3A" : "#c21818"}
          />
          <Row
            label="hint"
            value="Plays locally to verify assets are bundled in the IPA."
            hint
          />
        </Section>

        <View style={styles.testRow}>
          <TouchableOpacity
            style={[styles.testBtn, styles.testBtnCaf, cafPlaying && styles.testBtnActive]}
            onPress={cafPlaying ? stopBothSirens : playCaf}
            activeOpacity={0.85}
          >
            <Text style={styles.testBtnText}>
              {cafPlaying ? "Stop .caf" : "Play siren.caf"}
            </Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.testBtn, styles.testBtnMp3, mp3Playing && styles.testBtnActive]}
            onPress={mp3Playing ? stopBothSirens : playMp3}
            activeOpacity={0.85}
          >
            <Text style={styles.testBtnText}>
              {mp3Playing ? "Stop .mp3" : "Play siren.mp3"}
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
            {busy === "registering" ? "Re-registering…" : "Re-register with backend"}
          </Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={styles.btnGhost}
          onPress={onResetWatchReminder}
        >
          <Text style={styles.btnGhostText}>Reset Apple Watch reminder</Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={styles.btnGhost}
          onPress={() => router.back()}
        >
          <Text style={styles.btnGhostText}>Back</Text>
        </TouchableOpacity>

        <Text style={styles.footer}>
          Pull down to refresh. Long-press user_id / token fingerprint to share.
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
