import Route from '@ember/routing/route';
import { inject as service } from '@ember/service';
import Audited from 'sample-app/mixins/audited';
import { formatName } from 'sample-app/utils/format';

export default Route.extend(Audited, {
  queue: service('retry-queue'),
  session: service(),
  missingThing: service('not-in-this-repo'),

  enqueue() {
    return 'local enqueue, not the service';
  },

  model() {
    this.queue.enqueue('job');
    this.get('queue').flush();
    this.get('missingThing').poke();
    this.session.close();
    return this.store.findRecord('project', 1).then(formatName);
  }
});
