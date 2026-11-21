import Service, { inject as service } from '@ember/service';

export default Service.extend({
  queue: service('retry-queue'),

  record(entry) {
    return this.get('queue').enqueue(entry);
  },

  enqueue(entry) {
    return entry;
  }
});
