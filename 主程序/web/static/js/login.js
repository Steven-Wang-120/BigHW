'use strict';
const loginForm = document.getElementById('login-form');
loginForm.addEventListener('submit', event => {
  event.preventDefault();
  appApi.action(loginForm.querySelector('button'), async () => {
    try { const result = await appApi.request('/auth/login', appApi.json('POST', {username: appApi.$('username').value, password: appApi.$('password').value}));
      appApi.$('password').value = ''; location.assign(result.data.user.role === 'ADMIN' ? '/admin' : result.data.user.role === 'FINANCE' ? '/import' : '/results');
    } catch (error) { appApi.showError(appApi.$('message'), error, {redirect: false}); }
  });
});
