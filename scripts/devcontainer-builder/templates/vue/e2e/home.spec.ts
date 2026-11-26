import { test, expect } from "@playwright/test";

test("home page renders the Vue stack title", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: /Vue 3 \+ TypeScript \+ Vite Plus/ }),
  ).toBeVisible();
});
