import Route from '@ember/routing/route';

export default Route.extend({
  model(params) {
    return this.store.query('task', { project: params.project_id });
  }
});
