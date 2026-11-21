import Route from '@ember/routing/route';
import { service } from '@ember/service';

export default class ProjectsRoute extends Route {
  @service sessionState;
  @service('session-state') alias;
  @service store;

  model() {
    this.sessionState.refresh();
    this.alias.signOut();
    return this.store.findAll('project');
  }
}
