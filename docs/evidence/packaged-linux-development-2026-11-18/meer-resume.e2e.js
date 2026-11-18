const { launchApp } = require(process.cwd() + '/e2e/utils/launchApp');
describe('Packaged persistence', () => {
  it('retains authenticated Meer home without signing in again', async () => {
    await launchApp({ delete: false });
    await waitFor(element(by.id('home-search-button'))).toBeVisible().withTimeout(30000);
    await expect(element(by.id('login-email-input'))).not.toExist();
  });
});
