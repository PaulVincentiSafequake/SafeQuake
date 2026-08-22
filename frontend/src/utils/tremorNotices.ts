/**
 * #305 (2026-08-23 — Paul) — tremor notices: a chatty default, and the
 * self-correcting half that has to come with it.
 *
 * The change: new installs start on "Everything nearby — including tremors
 * too small to feel", not "Only shakes I would have felt".
 *
 * Paul's reasoning, recorded because the risk is the interesting part:
 *   "Malta goes years between felt earthquakes. If the app is silent for
 *    two years people forget it exists and delete it, and then it is not
 *    there when it matters. Small tremor notices keep it visible."
 *   "The risk that comes with it: a few notices a day about shakes nobody
 *    felt is exactly the pattern that makes someone open iOS settings and
 *    switch notifications off for the app entirely. That switch takes the
 *    siren with it. So a chatty default could quietly leave people
 *    unprotected while looking like engagement."
 *
 * So the app watches its own noise. It counts, locally and only locally,
 * how many tremor notices arrived and how many were opened. If a week has
 * passed and enough arrived and none were opened, it asks ONCE whether they
 * would like fewer.
 *
 * The rules, all enforced here so no screen can get them wrong:
 *   · Asked once, ever. A no is final.
 *   · Never asked at all if the person has already changed the setting
 *     themselves. They have decided.
 *   · Not asked on a quiet week. The threshold is 8 notices — more than one
 *     a day, which is the level Paul described as the annoying pattern, and
 *     enough of a sample that "opened none of them" means something. Under
 *     that we wait rather than ask about three messages.
 *   · Nothing here touches the siren, and the question must say so.
 *   · Never while an alert is live — the home screen owns that check.
 */
import AsyncStorage from "@react-native-async-storage/async-storage";

const KEY = "quakeangel_tremor_notices";

/** More than one a day for a week: enough noise to judge, and to annoy. */
export const ASK_THRESHOLD = 8;
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

export type TremorNoticeStats = {
  received: number;
  opened: number;
  /** ISO of the first notice we ever counted. The week runs from here. */
  firstAt: string | null;
  /** true once the person has changed the setting themselves, ever. */
  userChoseSetting: boolean;
  /** true once we have asked the quieten-down question. Never asked twice. */
  asked: boolean;
};

const EMPTY: TremorNoticeStats = {
  received: 0,
  opened: 0,
  firstAt: null,
  userChoseSetting: false,
  asked: false,
};

export async function getTremorNoticeStats(): Promise<TremorNoticeStats> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return { ...EMPTY };
    return { ...EMPTY, ...(JSON.parse(raw) as Partial<TremorNoticeStats>) };
  } catch {
    return { ...EMPTY };
  }
}

async function update(patch: Partial<TremorNoticeStats>): Promise<void> {
  try {
    const cur = await getTremorNoticeStats();
    await AsyncStorage.setItem(KEY, JSON.stringify({ ...cur, ...patch }));
  } catch {
    /* a lost count is not worth an error path — it only delays the question */
  }
}

export async function recordTremorNoticeReceived(): Promise<void> {
  const cur = await getTremorNoticeStats();
  await update({
    received: cur.received + 1,
    firstAt: cur.firstAt ?? new Date().toISOString(),
  });
}

export async function recordTremorNoticeOpened(): Promise<void> {
  const cur = await getTremorNoticeStats();
  await update({ opened: cur.opened + 1 });
}

/** Called whenever the person themselves picks a tremor setting. */
export async function markPresetChosenByUser(): Promise<void> {
  await update({ userChoseSetting: true });
}

export async function markQuietenAsked(): Promise<void> {
  await update({ asked: true });
}

/**
 * Should the home screen show the one-time "want fewer?" question?
 * Every condition is a reason NOT to ask, which is the right default.
 */
export async function shouldAskToQuieten(): Promise<boolean> {
  const s = await getTremorNoticeStats();
  if (s.asked) return false;              // once, ever
  if (s.userChoseSetting) return false;   // they have decided already
  if (s.opened > 0) return false;         // they are reading them
  if (s.received < ASK_THRESHOLD) return false;   // quiet week: wait
  if (!s.firstAt) return false;
  const started = new Date(s.firstAt).getTime();
  if (!Number.isFinite(started)) return false;
  return Date.now() - started >= WEEK_MS; // after their first week
}
