import Model, { attr, hasMany } from '@ember-data/model';

export default class Project extends Model {
  @attr title;
  @hasMany('project', { inverse: null, async: false }) related;
}
