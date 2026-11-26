export default {
  extends: ["stylelint-config-standard", "stylelint-config-recommended-vue"],
  ignoreFiles: ["dist/**", "node_modules/**"],
  rules: {
    // Tailwind CSS v4 使用 @import "tailwindcss"（不需要 url()）
    "import-notation": null,
  },
};
