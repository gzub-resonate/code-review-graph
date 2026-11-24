import Controller from '@ember/controller';
import { inject as controller } from '@ember/controller';

export default Controller.extend({
  other: controller('projects/show')
});
