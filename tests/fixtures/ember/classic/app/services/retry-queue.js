import Service from '@ember/service';

export default Service.extend({
  attempts: 0,

  enqueue(job) {
    this.attempts += 1;
    return job;
  },

  flush() {
    this.attempts = 0;
  }
});
