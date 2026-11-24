import EmberRouter from '@ember/routing/router';

const Router = EmberRouter.extend({});

Router.map(function () {
  this.route('projects', function () {
    this.route('show', { path: '/:project_id' });
    this.route('archive', { resetNamespace: true });
  });
  this.route('about', { path: '/about-us' });
  this.route('legacy-reports');
});

export default Router;
