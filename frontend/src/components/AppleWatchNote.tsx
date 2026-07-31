import { Ionicons } from "@expo/vector-icons";
import { StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing } from "@/src/theme";

interface Props {
  /**
   * "onboarding" → larger, high-emphasis card for the first-run flow.
   * "compact"    → dense, list-friendly card for /diag or settings screens.
   */
  variant?: "onboarding" | "compact";
}

/**
 * Single source of truth for the "iOS delivers alerts to your Apple Watch
 * instead of ringing your iPhone" advisory. Copy is intentionally identical
 * between onboarding and /diag so users see the same words in both places.
 */
export function AppleWatchNote({ variant = "compact" }: Props) {
  const isOnboarding = variant === "onboarding";
  const s = isOnboarding ? onboardingStyles : compactStyles;

  return (
    <View style={s.card} accessibilityRole="summary">
      <View style={s.header}>
        <View style={s.iconWrap}>
          <Ionicons
            name="watch-outline"
            size={isOnboarding ? 22 : 18}
            color={colors.warning}
          />
        </View>
        <Text style={s.title}>Wearing an Apple Watch?</Text>
      </View>

      <Text style={s.body}>
        If you wear an Apple Watch, iOS may deliver alerts to your Watch
        instead of ringing your iPhone loudly, even for critical alerts —
        this is an Apple system behavior, not a bug in the app.
      </Text>

      <View style={s.fixCard}>
        <Text style={s.fixLabel}>To make sure Quake Angel always rings your iPhone:</Text>
        <View style={s.stepRow}>
          <Text style={s.stepNum}>1.</Text>
          <Text style={s.stepText}>
            Open the <Text style={s.stepBold}>Watch</Text> app on your iPhone.
          </Text>
        </View>
        <View style={s.stepRow}>
          <Text style={s.stepNum}>2.</Text>
          <Text style={s.stepText}>
            Tap <Text style={s.stepBold}>Notifications</Text>.
          </Text>
        </View>
        <View style={s.stepRow}>
          <Text style={s.stepNum}>3.</Text>
          <Text style={s.stepText}>
            Find <Text style={s.stepBold}>Quake Angel</Text> in the list.
          </Text>
        </View>
        <View style={s.stepRow}>
          <Text style={s.stepNum}>4.</Text>
          <Text style={s.stepText}>
            Turn the toggle <Text style={s.stepBold}>OFF</Text>.
          </Text>
        </View>
      </View>

      <View style={s.updateNote}>
        <Text style={s.updateNoteLabel}>Heads up</Text>
        <Text style={s.updateNoteText}>
          iOS often <Text style={s.stepBold}>resets this toggle back to ON after
          app updates</Text> (including TestFlight installs and app-name/icon
          changes). Re-check this setting after every update — the app
          can&apos;t detect or change it for you.
        </Text>
      </View>
    </View>
  );
}

const baseStepRow = {
  flexDirection: "row" as const,
  alignItems: "flex-start" as const,
  gap: 8,
};

const onboardingStyles = StyleSheet.create({
  card: {
    backgroundColor: "#2A1F0A",
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: "#4A3814",
    padding: spacing.lg,
    gap: spacing.md,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  iconWrap: {
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: "#3B2C0F",
    alignItems: "center",
    justifyContent: "center",
  },
  title: {
    flex: 1,
    color: colors.onSurface,
    fontSize: 18,
    fontWeight: "800",
    letterSpacing: 0.3,
  },
  body: {
    color: colors.onSurfaceSecondary,
    fontSize: 15,
    lineHeight: 22,
  },
  fixCard: {
    backgroundColor: "#1F1608",
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: "#3A2A0E",
    padding: spacing.md,
    gap: spacing.xs,
  },
  fixLabel: {
    color: colors.warning,
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 4,
  },
  stepRow: baseStepRow,
  stepNum: {
    color: colors.warning,
    fontSize: 14,
    fontWeight: "800",
    width: 18,
  },
  stepText: {
    flex: 1,
    color: colors.onSurfaceSecondary,
    fontSize: 14,
    lineHeight: 20,
  },
  stepBold: {
    color: colors.onSurface,
    fontWeight: "700",
  },
  updateNote: {
    marginTop: 4,
    backgroundColor: "#1B1005",
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: "#2E1F08",
    padding: spacing.md,
    gap: 4,
  },
  updateNoteLabel: {
    color: "#E28A2B",
    fontSize: 12,
    fontWeight: "800",
    letterSpacing: 0.5,
    textTransform: "uppercase",
    marginBottom: 4,
  },
  updateNoteText: {
    color: colors.onSurfaceSecondary,
    fontSize: 14,
    lineHeight: 20,
  },
});

const compactStyles = StyleSheet.create({
  card: {
    backgroundColor: "#2A1F0A",
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "#4A3814",
    padding: 14,
    gap: 10,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  iconWrap: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: "#3B2C0F",
    alignItems: "center",
    justifyContent: "center",
  },
  title: {
    flex: 1,
    color: colors.onSurface,
    fontSize: 14,
    fontWeight: "700",
  },
  body: {
    color: colors.onSurfaceSecondary,
    fontSize: 13,
    lineHeight: 19,
  },
  fixCard: {
    backgroundColor: "#1F1608",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#3A2A0E",
    padding: 10,
    gap: 4,
  },
  fixLabel: {
    color: colors.warning,
    fontSize: 12,
    fontWeight: "700",
    marginBottom: 2,
  },
  stepRow: baseStepRow,
  stepNum: {
    color: colors.warning,
    fontSize: 12,
    fontWeight: "800",
    width: 16,
  },
  stepText: {
    flex: 1,
    color: colors.onSurfaceSecondary,
    fontSize: 12,
    lineHeight: 17,
  },
  stepBold: {
    color: colors.onSurface,
    fontWeight: "700",
  },
  updateNote: {
    marginTop: 2,
    backgroundColor: "#1B1005",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#2E1F08",
    padding: 10,
    gap: 2,
  },
  updateNoteLabel: {
    color: "#E28A2B",
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.5,
    textTransform: "uppercase",
    marginBottom: 2,
  },
  updateNoteText: {
    color: colors.onSurfaceSecondary,
    fontSize: 12,
    lineHeight: 17,
  },
});
