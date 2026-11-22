import EmberRouter from '@ember/routing/router';

export default class Router extends EmberRouter {}

Router.map(function () {
  this.route('projects', function () {
    this.route('show', { path: '/:id' });
  });
});
