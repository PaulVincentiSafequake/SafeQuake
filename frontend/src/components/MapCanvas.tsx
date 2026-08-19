/**
 * Web stub for MapCanvas — returns null.
 *
 * The seismic-map screen's web path uses a list layout instead of a
 * broken map, so this file only exists to satisfy Metro's import
 * resolver on web. The real implementation is in MapCanvas.native.tsx
 * and Metro picks that automatically on iOS/Android.
 *
 * Why a file split at all: react-native-maps imports react-native
 * internals (`Libraries/Utilities/codegenNativeCommands`) that Metro
 * refuses to serve to web bundles, even from inside a Platform.OS
 * guard. So the import path itself has to be platform-conditional.
 */
import { forwardRef } from "react";
import type { MapCanvasProps, MapCanvasHandle } from "./MapCanvas.types";

export default forwardRef<MapCanvasHandle, MapCanvasProps>(function MapCanvas(_props, _ref): null {
  // The imperative handle is a no-op on web — the seismic-map screen
  // uses a list layout there, so animateToWideView has nothing to do.
  return null;
});
