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
  registerForPushNotifications,
  getDiagInfo,
  type DiagInfo,
} from "@/src/utils/push";

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
          <Row label="platform" value={info?.platform?.toUpperCase() ?? "—"} />
          <Row label="app version" value={info?.app_version ?? "—"} />
          <Row label="build number" value={info?.build_number ?? "—"} />
        </Section>

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
