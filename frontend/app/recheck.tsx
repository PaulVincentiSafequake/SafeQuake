/**
 * C1 — the re-check screen. One question, four buttons, nothing else.
 *
 * This is the SECONDARY answer path. The primary one is the lock-screen
 * notification buttons, which submit without opening the app at all, because
 * asking someone badly injured to get past Face ID in the dark under dust is
 * exactly the wrong thing to ask (Paul, 2026-08-18). This screen exists for
 * anyone who taps the notification body instead, or opens the app later.
 *
 * Buttons are ~64pt tall, full width, no forms, no free text, no second step.
 * Order: SAME · WORSE · MUCH WORSE · BETTER. MUCH WORSE reaches red in one
 * tap from any band — a single WORSE button cannot express how much worse.
 */
import { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, TouchableOpacity, ActivityIndicator, ScrollView,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import * as Haptics from "expo-haptics";

import { getDeviceId } from "@/src/utils/checkin";
import {
  flushRecheckQueue,
  pendingRecheckAnswers,
  submitRecheckAnswer,
  type RecheckAnswer,
} from "@/src/utils/recheck";

const OPTIONS: {
  answer: RecheckAnswer;
  label: string;
  helper: string;
  bg: string;
  fg: string;
}[] = [
  {
    answer: "same",
    label: "SAME",
    helper: "No change since last time",
    bg: "#2B3A55", fg: "#FFFFFF",
  },
  {
    answer: "worse",
    label: "WORSE",
    helper: "Getting worse",
    bg: "#C97A11", fg: "#FFFFFF",
  },
  {
    answer: "much_worse",
    label: "MUCH WORSE",
    helper: "Urgent — much worse than before",
    bg: "#C21818", fg: "#FFFFFF",
  },
  {
    answer: "better",
    label: "BETTER",
    helper: "Improving",
    bg: "#1F6F3A", fg: "#FFFFFF",
  },
];

export default function RecheckScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ check_id?: string }>();
  const [sending, setSending] = useState<RecheckAnswer | null>(null);
  const [done, setDone] = useState<{ answer: RecheckAnswer; queued: boolean } | null>(null);
  const [pending, setPending] = useState(0);

  useEffect(() => {
    // Any answer tapped while offline goes out the moment this screen opens.
    flushRecheckQueue()
      .then(() => pendingRecheckAnswers())
      .then(setPending)
      .catch(() => {});
  }, []);

  const answer = useCallback(async (choice: RecheckAnswer) => {
    setSending(choice);
    Haptics.impactAsync(
      choice === "much_worse"
        ? Haptics.ImpactFeedbackStyle.Heavy
        : Haptics.ImpactFeedbackStyle.Medium,
    ).catch(() => {});
    try {
      const deviceId = await getDeviceId();
      const res = await submitRecheckAnswer(deviceId, choice, params.check_id);
      setDone({ answer: choice, queued: !res.delivered });
      setPending(await pendingRecheckAnswers());
    } finally {
      setSending(null);
    }
  }, [params.check_id]);

  if (done) {
    return (
      <SafeAreaView style={styles.container} edges={["top", "bottom"]}>
        <View style={styles.doneWrap}>
          <Ionicons name="checkmark-circle" size={64} color="#34C759" />
          <Text style={styles.doneTitle}>Thank you — that&apos;s recorded.</Text>
          <Text style={styles.doneBody}>
            {done.queued
              ? "You have no signal right now. Your answer is saved with the time you tapped it and will be sent the moment signal returns."
              : "The rescue team can see your update."}
          </Text>
          <Text style={styles.doneBody}>
            We&apos;ll check again a little later. Stay where you are if it is safe to do so.
          </Text>
          <TouchableOpacity
            style={styles.closeBtn}
            onPress={() => (router.canGoBack() ? router.back() : router.replace("/"))}
            accessibilityRole="button"
          >
            <Text style={styles.closeBtnText}>Close</Text>
          </TouchableOpacity>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.container} edges={["top", "bottom"]}>
      <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
        <Text style={styles.title}>Are you still OK?</Text>
        <Text style={styles.sub}>
          Has anything changed since you told us you were trapped? One tap is enough.
        </Text>

        {OPTIONS.map((o) => (
          <TouchableOpacity
            key={o.answer}
            style={[styles.btn, { backgroundColor: o.bg }]}
            onPress={() => answer(o.answer)}
            disabled={sending !== null}
            accessibilityRole="button"
            accessibilityLabel={`${o.label} — ${o.helper}`}
            testID={`recheck-${o.answer}`}
          >
            {sending === o.answer ? (
              <ActivityIndicator color={o.fg} />
            ) : (
              <>
                <Text style={[styles.btnLabel, { color: o.fg }]}>{o.label}</Text>
                <Text style={[styles.btnHelper, { color: o.fg }]}>{o.helper}</Text>
              </>
            )}
          </TouchableOpacity>
        ))}

        {pending > 0 && (
          <Text style={styles.pending}>
            {pending} earlier answer{pending === 1 ? "" : "s"} still waiting for signal — saved
            with the time you tapped, nothing is lost.
          </Text>
        )}

        <Text style={styles.footer}>
          You can also answer straight from the notification on your lock screen,
          without unlocking your phone.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#0B1220" },
  scroll: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 28 },
  title: { color: "#E7EDF5", fontSize: 30, fontWeight: "800" },
  sub: { color: "#8FA0BC", fontSize: 15, lineHeight: 21, marginTop: 8, marginBottom: 20 },
  btn: {
    minHeight: 64,
    borderRadius: 14,
    paddingHorizontal: 18,
    paddingVertical: 12,
    marginBottom: 12,
    justifyContent: "center",
  },
  btnLabel: { fontSize: 20, fontWeight: "900", letterSpacing: 0.5 },
  btnHelper: { fontSize: 13, opacity: 0.9, marginTop: 2 },
  pending: { color: "#F4C842", fontSize: 13, lineHeight: 19, marginTop: 8 },
  footer: { color: "#8FA0BC", fontSize: 13, lineHeight: 19, marginTop: 18 },
  doneWrap: { flex: 1, alignItems: "center", justifyContent: "center", paddingHorizontal: 24, gap: 14 },
  doneTitle: { color: "#E7EDF5", fontSize: 24, fontWeight: "800", textAlign: "center" },
  doneBody: { color: "#8FA0BC", fontSize: 15, lineHeight: 22, textAlign: "center" },
  closeBtn: {
    marginTop: 16, minHeight: 52, paddingHorizontal: 32, borderRadius: 12,
    borderWidth: 1, borderColor: "rgba(255,255,255,0.18)", justifyContent: "center",
  },
  closeBtnText: { color: "#E7EDF5", fontSize: 16, fontWeight: "700" },
});
