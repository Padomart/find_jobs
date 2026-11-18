import { defineConfig } from "mygo-cli";

export default defineConfig({
  name: "Job Panel",
  // codesign падает на кириллице в имени бандла, поэтому имя латиницей, а в Dock и меню - по-русски
  macos: { infoPlist: { CFBundleDisplayName: "Панель вакансий", CFBundleName: "Панель вакансий" } },
  identifier: "dev.findjobs.panel",
  version: "0.1.0",
  // `mygo dev` runs devCommand and loads devUrl; `mygo build` runs
  // buildCommand and embeds frontendDist into the app.
  devUrl: "http://localhost:5173",
  devCommand: "bun run dev:web",
  buildCommand: "bun run build:web",
  frontendDist: "dist",
  bindings: "src/mygo.ts",
  out: "build",
  // Phones: `mygo dev -platform ios` and `-platform android` run the app on a
  // simulator, an emulator or a device (`mygo devices` lists them, and
  // `mygo doctor` tells what is missing). What devices and stores need:
  // ios: { team: "ABCDE12345", infoPlist: { NSCameraUsageDescription: "Scan receipts." } },
  // android: { keystore: "upload.jks", keyAlias: "upload", permissions: ["CAMERA"] },
});
